"""Handing the household's hardware back - what Ohmter holds, and nothing else.

The bridge never keeps a state Ohmter asked for once Ohmter has let go of the
device. Ohmter lets go in more ways than going quiet, and every one of them
ends in the same hand-back:

* Ohmter has not answered for HOLD_TIMEOUT_SECONDS (`releases_due`);
* the accepted mapping drops a device, points a role at another entity, or
  the household withdraws the grant (`mapping_handoffs`);
* the household deletes the device in Ohmter (a server command marked
  ``release``), Ohmter revokes the bridge, or the integration is removed from
  Home Assistant (the coordinator and `async_remove_entry`).

What a hand-back does is decided per device, from the memory the executor
writes, and is limited to what Ohmter itself changed:

* a charger Ohmter switched *off* is switched back on - a charger left on
  costs some expensive kWh, a charger left off strands a car in the morning -
  while one Ohmter left on, or never touched, is not touched;
* a max current Ohmter lowered is put back to what it was before Ohmter's
  first write;
* a thermostat Ohmter set is restored to what the household had.

When a hand-back runs, the executor also checks the entity is still where
Ohmter left it: a household that changed it since owns it, and the hand-back
does not undo their change.

Pure: it reads the per-device memory and returns synthetic commands for the
executor to run. Nothing here talks to Home Assistant or to Ohmter, which is
what makes it testable on a fake clock, and what makes it run whether or not
Ohmter ever comes back.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .protocol import HOLD_TIMEOUT_SECONDS, HOLDABLE_ROLES, RELEASE_PARAM

# The role the household's grant covers, per kind. The current limit is not
# part of the grant (see features_for on the server): mapping a `number` is
# what allows it.
GRANT_ROLE = {"ev": "switch", "thermostat": "climate"}


def _parse(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def ohmter_is_quiet(last_ok_sync: Optional[str], now: datetime.datetime, hold_timeout: int = HOLD_TIMEOUT_SECONDS) -> bool:
    last = _parse(last_ok_sync)
    if last is None:
        return True
    if (last.tzinfo is None) != (now.tzinfo is None):
        last, now = last.replace(tzinfo=None), now.replace(tzinfo=None)
    return (now - last).total_seconds() > hold_timeout


def release_commands(
    device: Dict[str, Any],
    memory: Optional[Dict[str, Any]],
    roles: Optional[Set[str]] = None,
    *,
    first_id: int = -1,
    label: str = "release",
) -> List[Dict[str, Any]]:
    """The commands that hand one device back, from what Ohmter did to it.

    `roles` narrows it to the roles Ohmter is losing; None means all of them.
    A read-only kind has no holdable role, so it never has anything to hand
    back. Each command carries ``params.release``, which is what tells the executor
    to run it as a hand-back: without the grant (a hand-back is Ohmter letting
    go, not Ohmter controlling) and only while the entity is where Ohmter left
    it.
    """
    mem = memory or {}
    kind = str(device.get("kind") or "")
    wanted = set(HOLDABLE_ROLES.get(kind, ())) if roles is None else set(roles)
    key = str(device.get("device_key") or "")
    out: List[Dict[str, Any]] = []

    def add(action: str, params: Dict[str, Any]) -> None:
        out.append(_synthetic(key, action, {**params, RELEASE_PARAM: True}, first_id - len(out), label))

    if kind == "ev":
        if "switch" in wanted and switch_set_by_ohmter(mem) == "off":
            add("on", {})
        # current_before is written only when Ohmter writes the number, so it
        # is itself the proof that the current is Ohmter's to put back.
        if "current" in wanted and mem.get("current_before") is not None:
            add("set_current", {"amps": mem["current_before"]})
    elif kind == "thermostat":
        if "climate" in wanted and mem.get("thermostat_before"):
            add("restore", {})
    return out


def switch_set_by_ohmter(memory: Dict[str, Any]) -> Optional[str]:
    """What Ohmter last did to the charging switch: "on", "off" or nothing.

    `switch_set` is kept on its own because `last_action` is shared with the
    current limit: a pause followed by a load-balancing write used to leave
    `last_action` at set_current, and the charger Ohmter was holding off was
    never switched back on. A memory from before `switch_set` existed has only
    `last_action` to go on.
    """
    if "switch_set" in memory:
        return memory.get("switch_set")
    last = memory.get("last_action")
    return last if last in ("on", "off") else None


def releases_due(
    devices: List[Dict[str, Any]],
    memories: Dict[str, Dict[str, Any]],
    *,
    last_ok_sync: Optional[str],
    now: datetime.datetime,
    hold_timeout: int = HOLD_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """Synthetic commands that hand each device back once Ohmter has gone quiet.

    Each is shaped like a server command (`id` negative so it can never
    collide with a real one, `source: "watchdog"`), executed through the
    ordinary executor, and never acknowledged to Ohmter. Once a device has
    been released its memory says so, so the release runs once per outage.
    """
    if not ohmter_is_quiet(last_ok_sync, now, hold_timeout):
        return []
    out: List[Dict[str, Any]] = []
    for d in devices:
        mem = memories.get(d["device_key"]) or {}
        if mem.get("released_at"):
            continue
        out.extend(release_commands(d, mem, first_id=-(len(out) + 1), label="ohmter_quiet_release"))
    return out


def _synthetic(key: str, action: str, params: Dict[str, Any], cid: int, label: str) -> Dict[str, Any]:
    return {
        "id": cid,
        "device_key": key,
        "action": action,
        "params": params,
        "source": "watchdog",
        "label": label,
        "expires_at": None,
    }


def mark_released(memory: Dict[str, Any], now: datetime.datetime) -> None:
    memory["released_at"] = now.isoformat()
    memory["last_action"] = None
    memory["switch_set"] = None
    memory["current_before"] = None
    memory["current_written"] = None
    memory["thermostat_before"] = None
    memory["thermostat_set"] = None
    # A release is not a normal write; the interval guard must not block the
    # next real command after Ohmter returns.
    memory["last_number_write_at"] = None


def mark_ohmter_back(memories: Dict[str, Dict[str, Any]]) -> None:
    for mem in memories.values():
        mem.pop("released_at", None)


def forget(memory: Dict[str, Any], roles: Set[str]) -> None:
    """Drop what the bridge remembers about roles it no longer controls."""
    if "switch" in roles:
        memory["switch_set"] = None
        if memory.get("last_action") in ("on", "off"):
            memory["last_action"] = None
    if "current" in roles:
        memory["current_before"] = None
        memory["current_written"] = None
        memory["last_number_write_at"] = None
        if memory.get("last_action") == "set_current":
            memory["last_action"] = None
    if "climate" in roles:
        memory["thermostat_before"] = None
        memory["thermostat_set"] = None
        if memory.get("last_action") in ("set_temperature", "restore"):
            memory["last_action"] = None


def _granted(device: Dict[str, Any]) -> bool:
    """Whether the household's grant covers this device's control entity.

    A mapping from before the confirmation step carries no flag and keeps the
    control it had, exactly as the server reads it.
    """
    role = GRANT_ROLE.get(str(device.get("kind") or ""))
    if not role or not (device.get("entities") or {}).get(role):
        return False
    confirmed = device.get("control_confirmed")
    return True if confirmed is None else bool(confirmed)


def lost_roles(old: Dict[str, Any], new: Optional[Dict[str, Any]]) -> Set[str]:
    """The roles Ohmter stops controlling on `old` when the mapping becomes `new`.

    A role is lost when the device is gone, when the role now points at
    another entity or at none, or - for the role the grant covers - when the
    household withdrew the grant.
    """
    kind = str(old.get("kind") or "")
    holdable = HOLDABLE_ROLES.get(kind, ())
    if new is None or str(new.get("kind") or "") != kind:
        return set(holdable)
    before, after = old.get("entities") or {}, new.get("entities") or {}
    lost = {r for r in holdable if before.get(r) and before.get(r) != after.get(r)}
    if _granted(old) and not _granted(new):
        lost.add(GRANT_ROLE[kind])
    return lost


def mapping_handoffs(
    memories: Dict[str, Dict[str, Any]],
    old_devices: List[Dict[str, Any]],
    new_devices: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]], Set[str], bool]]:
    """What the bridge hands back when the accepted mapping changes.

    One entry per device Ohmter holds something on: the *old* device - the
    old entity is the one Ohmter changed - the release commands for the roles
    it is losing, those roles, and whether the device is gone altogether. The
    caller runs the commands, then calls `forget` (or drops the memory of a
    device that is gone). The entity a role now points at is never touched: a
    compensating command on an entity Ohmter never switched is the thing this
    exists to prevent.
    """
    new_by_key = {d.get("device_key"): d for d in (new_devices or [])}
    out = []
    for old in old_devices or []:
        key = old.get("device_key")
        mem = memories.get(key)
        if not mem:
            continue
        new = new_by_key.get(key)
        roles = lost_roles(old, new)
        if not roles:
            continue
        commands = release_commands(old, mem, roles, first_id=-(len(out) * 10 + 1), label="mapping_handoff")
        out.append((old, commands, roles, new is None))
    return out


def drop_orphaned_memory(
    memories: Dict[str, Dict[str, Any]],
    old_devices: List[Dict[str, Any]],
    new_devices: List[Dict[str, Any]],
) -> List[str]:
    """Drop the memory of a key that comes back without having been handed back.

    `device_key` is the slug of the name, kept unique only against the devices
    that exist, so deleting "Garage charger" and adding another one frees the
    key for a different entity. A removal hands back and forgets through
    `mapping_handoffs`; this catches a memory that outlived it anyway - written
    by an integration from before hand-backs existed. Nothing proves the new
    device is the old target, so the memory is dropped, never acted on.
    """
    old_keys = {d.get("device_key") for d in (old_devices or [])}
    dropped = []
    for d in new_devices or []:
        key = d.get("device_key")
        if key in old_keys or not memories.get(key):
            continue
        memories.pop(key, None)
        dropped.append(key)
    return dropped
