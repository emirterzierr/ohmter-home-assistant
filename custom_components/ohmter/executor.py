"""Run one Ohmter command as Home Assistant service calls.

Pure apart from two injected callables: `get_state(entity_id)` and
`async call_service(domain, service, data)`. The memory it keeps - what a
thermostat was set to before Ohmter touched it and what Ohmter set it to,
what a `number` was before Ohmter's first write and what Ohmter last wrote -
is a plain dict so the integration can persist it and the watchdog can read
it.

Four rules from docs/HOME_ASSISTANT_BRIDGE.md are enforced here:

* An expired command is refused before anything is called.
* A thermostat window *restores what was there*, not "eco": the baseline is
  captured on the first set of a window and cleared on restore - and only
  while the thermostat is still at what Ohmter set. A change the household
  made in the meantime is theirs, and a restore does not undo it.
* A `number` entity is written at most once per MIN_NUMBER_WRITE_INTERVAL,
  because a charger behind Home Assistant may still be a Zaptec.
* A hand-back (``params.release``) is Ohmter letting go, not Ohmter
  controlling: it runs without the household's grant - a withdrawn grant is
  one of the reasons it runs - and it touches an entity only while that
  entity is where Ohmter left it.
"""
from __future__ import annotations

import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .protocol import ACTIONS_BY_KIND, COMMAND_TARGET_FIELD, MIN_NUMBER_WRITE_INTERVAL_SECONDS, RELEASE_PARAM

CallService = Callable[[str, str, Dict[str, Any]], Awaitable[None]]
GetState = Callable[[str], Any]
IsReplaced = Callable[[Dict[str, Any], str], Optional[str]]

# How far an entity may drift from what Ohmter wrote and still count as
# Ohmter's: a charger reports its current in whole amps, a thermostat its
# target in halves.
_AMPS_TOLERANCE = 0.5
_TEMP_TOLERANCE = 0.25


class CommandRefused(Exception):
    """Not executed, and the reason is the ack's error text."""


def _parse(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt


def _same_clock(a: datetime.datetime, b: datetime.datetime) -> "tuple[datetime.datetime, datetime.datetime]":
    """Compare naive with naive: the server sends naive Amsterdam wall-clock."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=None)
        b = b.replace(tzinfo=None)
    return a, b


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_release(command: Dict[str, Any]) -> bool:
    return bool((command.get("params") or {}).get(RELEASE_PARAM))


async def execute(
    command: Dict[str, Any],
    device: Dict[str, Any],
    *,
    get_state: GetState,
    call_service: CallService,
    memory: Dict[str, Any],
    now: datetime.datetime,
    executed_ids: set,
) -> str:
    """Run `command` against `device`; returns a short note for the log.

    Raises CommandRefused with the reason when it must not or cannot run.
    `memory` is per device_key and mutated in place.
    """
    cid = int(command["id"])
    if cid in executed_ids:
        return "already executed"
    kind = str(device["kind"])
    action = str(command.get("action") or "")
    release = is_release(command)
    if action not in ACTIONS_BY_KIND.get(kind, ()):
        raise CommandRefused(f"{action} is not an action for a {kind}")
    # The server names the entity the command was decided for. If this
    # mapping points that role somewhere else - it was edited after Ohmter
    # queued the command, or this is a sync from before the edit - the
    # command is not run against whatever the role points at now.
    target = command.get(COMMAND_TARGET_FIELD)
    if isinstance(target, dict) and target.get("role") and target.get("entity_id"):
        mapped = (device.get("entities") or {}).get(str(target["role"]))
        if mapped != target["entity_id"]:
            raise CommandRefused("the mapping changed in Home Assistant before this command ran, so it was not run")
    # The household grants control in so many words when they map the device
    # (a legacy mapping with no flag keeps what it had). A switch that is
    # merely mapped is watched, never switched; a missing `number` means no
    # current limit. The server refuses these too; this is the last door. A
    # hand-back passes it: it only ever undoes what Ohmter itself set.
    entities = device.get("entities") or {}
    confirmed = device.get("control_confirmed")
    if action in ("on", "off"):
        if not entities.get("switch"):
            raise CommandRefused("no charging switch is mapped for this device in Home Assistant")
        if confirmed is False and not release:
            raise CommandRefused("charging control was not confirmed for this device in Home Assistant")
    elif action == "set_current" and not entities.get("current"):
        raise CommandRefused("no charge-current entity is mapped for this device in Home Assistant")
    elif action in ("set_temperature", "restore") and confirmed is False and not release:
        raise CommandRefused("thermostat control was not confirmed for this device in Home Assistant")
    expires = _parse(command.get("expires_at"))
    if expires is not None:
        a, b = _same_clock(now, expires)
        if a >= b:
            raise CommandRefused("expired before the bridge ran it")
    params = command.get("params") or {}

    if kind == "ev":
        note = await _execute_ev(action, params, entities, get_state, call_service, memory, now, release)
    else:
        note = await _execute_thermostat(action, params, entities, get_state, call_service, memory)
    executed_ids.add(cid)
    if not release:
        memory["last_action"] = action
        memory["last_action_at"] = now.isoformat()
    return note


async def _execute_ev(action, params, entities, get_state, call_service, memory, now, release) -> str:
    switch = entities.get("switch")
    if action in ("on", "off"):
        if not switch:
            raise CommandRefused("no charging switch mapped")
        st = get_state(switch)
        if st is None or st.state in ("unavailable", "unknown"):
            raise CommandRefused(f"{switch} is unavailable - the charger's own mode may be in control")
        if release and st.state == action:
            return f"{switch} already {action}"
        await call_service("switch", "turn_on" if action == "on" else "turn_off", {"entity_id": switch})
        if not release:
            # Kept apart from last_action: a current write after a pause must
            # not hide that Ohmter is holding the charger off.
            memory["switch_set"] = action
        return f"switch.{'turn_on' if action == 'on' else 'turn_off'} {switch}"
    if action == "set_current":
        number = entities.get("current")
        if not number:
            raise CommandRefused("no max-current entity mapped")
        amps = _num(params.get("amps"))
        if amps is None or amps < 0:
            raise CommandRefused("set_current without a valid amps value")
        st = get_state(number)
        if st is None or st.state in ("unavailable", "unknown"):
            raise CommandRefused(f"{number} is unavailable")
        attrs = st.attributes or {}
        lo, hi = _num(attrs.get("min")), _num(attrs.get("max"))
        if lo is not None and amps < lo:
            amps = lo
        if hi is not None and amps > hi:
            amps = hi
        current = _num(st.state)
        if release:
            # Put back only a value that is still Ohmter's. A memory from
            # before Ohmter recorded what it wrote cannot tell, and hands back.
            written = _num(memory.get("current_written"))
            if written is not None and current is not None and abs(current - written) >= _AMPS_TOLERANCE:
                return f"{number} changed since Ohmter set it; left at {current:g}"
            await call_service("number", "set_value", {"entity_id": number, "value": amps})
            memory["last_number_write_at"] = None
            return f"number.set_value {number}={amps}"
        last = _parse(memory.get("last_number_write_at"))
        if last is not None:
            a, b = _same_clock(now, last)
            if (a - b).total_seconds() < MIN_NUMBER_WRITE_INTERVAL_SECONDS:
                if current is not None and abs(current - amps) < _AMPS_TOLERANCE:
                    memory["current_written"] = amps
                    return "current already at value"
                raise CommandRefused("max current was written less than a minute ago")
        if memory.get("current_before") is None:
            memory["current_before"] = current
        await call_service("number", "set_value", {"entity_id": number, "value": amps})
        memory["last_number_write_at"] = now.isoformat()
        memory["current_written"] = amps
        return f"number.set_value {number}={amps}"
    raise CommandRefused(f"unknown EV action {action}")


def _changed_since_ohmter(set_by_ohmter: Dict[str, Any], st: Any) -> bool:
    """Whether the thermostat has moved away from what Ohmter last set."""
    attrs = st.attributes or {}
    mode = set_by_ohmter.get("hvac_mode")
    if mode and str(st.state) != str(mode):
        return True
    wanted, actual = _num(set_by_ohmter.get("temperature")), _num(attrs.get("temperature"))
    return wanted is not None and actual is not None and abs(actual - wanted) >= _TEMP_TOLERANCE


async def _execute_thermostat(action, params, entities, get_state, call_service, memory) -> str:
    climate = entities.get("climate")
    if not climate:
        raise CommandRefused("no climate entity mapped")
    st = get_state(climate)
    if st is None or st.state in ("unavailable", "unknown"):
        raise CommandRefused(f"{climate} is unavailable")
    attrs = st.attributes or {}
    if action == "set_temperature":
        target = _num(params.get("target_temp_c"))
        mode = params.get("mode")
        if target is None and not mode:
            raise CommandRefused("set_temperature with neither a target nor a mode")
        # The baseline is what the household had before Ohmter's window, so
        # a second set inside the same window must not overwrite it.
        if memory.get("thermostat_before") is None:
            memory["thermostat_before"] = {
                "temperature": _num(attrs.get("temperature")),
                "hvac_mode": str(st.state),
                "preset_mode": attrs.get("preset_mode"),
            }
        notes = []
        applied_mode = None
        if mode and mode in (attrs.get("hvac_modes") or []):
            await call_service("climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": mode})
            notes.append(f"hvac_mode={mode}")
            applied_mode = mode
        if target is not None:
            lo, hi = _num(attrs.get("min_temp")), _num(attrs.get("max_temp"))
            if lo is not None and target < lo:
                target = lo
            if hi is not None and target > hi:
                target = hi
            await call_service("climate", "set_temperature", {"entity_id": climate, "temperature": target})
            notes.append(f"temperature={target}")
        memory["thermostat_set"] = {"temperature": target, "hvac_mode": applied_mode}
        return "climate " + " ".join(notes)
    if action == "restore":
        before = memory.get("thermostat_before")
        if not before:
            return "nothing to restore"
        set_by_ohmter = memory.get("thermostat_set")
        if set_by_ohmter and _changed_since_ohmter(set_by_ohmter, st):
            memory["thermostat_before"] = None
            memory["thermostat_set"] = None
            return f"{climate} changed since Ohmter set it; left as the household has it"
        if before.get("hvac_mode") and before["hvac_mode"] in (attrs.get("hvac_modes") or [before["hvac_mode"]]):
            await call_service("climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": before["hvac_mode"]})
        if before.get("preset_mode") and before["preset_mode"] in (attrs.get("preset_modes") or []):
            await call_service("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": before["preset_mode"]})
        if before.get("temperature") is not None:
            await call_service("climate", "set_temperature", {"entity_id": climate, "temperature": before["temperature"]})
        memory["thermostat_before"] = None
        memory["thermostat_set"] = None
        return f"climate restored to {before}"
    raise CommandRefused(f"unknown thermostat action {action}")


async def hand_back(
    device: Dict[str, Any],
    commands: List[Dict[str, Any]],
    *,
    get_state: GetState,
    call_service: CallService,
    memory: Dict[str, Any],
    now: datetime.datetime,
    is_replaced: Optional[IsReplaced] = None,
) -> List[Tuple[bool, str]]:
    """Run one device's hand-back commands; never raises.

    Returns (done, note) per command. Nothing left to hand back is done: the
    entity is already where the household wants it, they changed it since, or
    Home Assistant replaced it under the same id since it was mapped - so
    Ohmter never touched the one there now. An entity that is unavailable, or
    a service call that fails, is not done, and the caller keeps what it
    remembers so a later hand-back can still run.
    """
    results: List[Tuple[bool, str]] = []
    for cmd in commands:
        action = str(cmd.get("action") or "")
        replaced = is_replaced(device, action) if is_replaced is not None else None
        if replaced:
            results.append((True, f"{action}: nothing to hand back, {replaced} was replaced in Home Assistant"))
            continue
        try:
            results.append((True, await execute(
                cmd, device, get_state=get_state, call_service=call_service,
                memory=memory, now=now, executed_ids=set(),
            )))
        except CommandRefused as exc:
            results.append((False, f"{action}: not run, {exc}"))
        except Exception as exc:  # noqa: BLE001 - one failed service call must not stop the next hand-back
            results.append((False, f"{action}: failed, {str(exc)[:160]}"))
    return results
