"""When Ohmter goes quiet, the household's own hardware settings win.

The bridge never holds a state Ohmter asked for once Ohmter has stopped
answering. Concretely, after HOLD_TIMEOUT_SECONDS without a successful sync:

* a charger Ohmter switched *off* is switched back on, and a max current
  Ohmter lowered is put back to what it was - a charger left on costs some
  expensive kWh, a charger left off strands a car in the morning;
* a thermostat Ohmter set is restored to what the household had.

Pure: it reads the per-device memory the executor writes and returns
synthetic commands for the executor to run. Nothing here talks to Home
Assistant or to Ohmter, which is what makes it testable on a fake clock, and
what makes it run whether or not Ohmter ever comes back.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from .protocol import HOLD_TIMEOUT_SECONDS


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


def releases_due(
    devices: List[Dict[str, Any]],
    memories: Dict[str, Dict[str, Any]],
    *,
    last_ok_sync: Optional[str],
    now: datetime.datetime,
    hold_timeout: int = HOLD_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """Synthetic commands that hand each device back to the household.

    Each is shaped like a server command (`id` negative so it can never
    collide with a real one, `source: "watchdog"`), executed through the
    ordinary executor, and never acknowledged to Ohmter. Once a device has
    been released its memory says so, so the release runs once per outage.
    """
    if not ohmter_is_quiet(last_ok_sync, now, hold_timeout):
        return []
    out: List[Dict[str, Any]] = []
    for d in devices:
        key = d["device_key"]
        mem = memories.get(key) or {}
        if mem.get("released_at"):
            continue
        kind = d["kind"]
        if kind == "ev":
            if mem.get("last_action") == "off":
                out.append(_synthetic(key, "on", {}, len(out)))
            if mem.get("current_before") is not None and mem.get("last_action") in ("set_current", "off", "on"):
                out.append(_synthetic(key, "set_current", {"amps": mem["current_before"], "_release": True}, len(out)))
        elif kind == "thermostat":
            if mem.get("thermostat_before"):
                out.append(_synthetic(key, "restore", {}, len(out)))
    return out


def _synthetic(key: str, action: str, params: Dict[str, Any], n: int) -> Dict[str, Any]:
    return {
        "id": -(n + 1),
        "device_key": key,
        "action": action,
        "params": params,
        "source": "watchdog",
        "label": "ohmter_quiet_release",
        "expires_at": None,
    }


def mark_released(memory: Dict[str, Any], now: datetime.datetime) -> None:
    memory["released_at"] = now.isoformat()
    memory["last_action"] = None
    memory["current_before"] = None
    memory["thermostat_before"] = None
    # A release is not a normal write; the interval guard must not block the
    # next real command after Ohmter returns.
    memory["last_number_write_at"] = None


def mark_ohmter_back(memories: Dict[str, Dict[str, Any]]) -> None:
    for mem in memories.values():
        mem.pop("released_at", None)


def forget_changed_targets(
    memories: Dict[str, Dict[str, Any]],
    old_devices: List[Dict[str, Any]],
    new_devices: List[Dict[str, Any]],
) -> List[str]:
    """Drop what the bridge remembers about an entity a device no longer maps.

    The memory is kept per device, and the watchdog acts on it. Left alone
    after an Edit that points the charging switch at another entity, a
    remembered "off" for the old switch would make the release turn the *new*
    switch on - a compensating command on an entity Ohmter never touched.
    Returns the device keys whose memory changed.
    """
    old_by_key = {d.get("device_key"): d for d in (old_devices or [])}
    touched = []
    for d in new_devices or []:
        key = d.get("device_key")
        mem = memories.get(key)
        if not mem:
            continue
        old = old_by_key.get(key)
        if old is None:
            # A key that carries a memory but was not in the accepted mapping
            # has been removed and made again - `device_key` is the slug of the
            # name and is only kept unique against the devices that exist, so
            # deleting "Garage charger" and adding another one frees the key for
            # a different entity. Nothing here proves it is the same target, so
            # the rule the server uses applies: do not assume it is.
            mem["last_action"] = None
            mem["current_before"] = None
            mem["last_number_write_at"] = None
            mem["thermostat_before"] = None
            touched.append(key)
            continue
        before, after = old.get("entities") or {}, d.get("entities") or {}
        changed = False
        if before.get("switch") != after.get("switch") and mem.get("last_action") in ("on", "off"):
            mem["last_action"] = None
            changed = True
        if before.get("current") != after.get("current"):
            if mem.get("current_before") is not None or mem.get("last_number_write_at"):
                mem["current_before"] = None
                mem["last_number_write_at"] = None
                changed = True
            if mem.get("last_action") == "set_current":
                mem["last_action"] = None
                changed = True
        if before.get("climate") != after.get("climate") and (
                mem.get("thermostat_before") or mem.get("last_action") in ("set_temperature", "restore")):
            mem["thermostat_before"] = None
            if mem.get("last_action") in ("set_temperature", "restore"):
                mem["last_action"] = None
            changed = True
        if changed:
            touched.append(key)
    return touched
