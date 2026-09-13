"""Run one Ohmter command as Home Assistant service calls.

Pure apart from two injected callables: `get_state(entity_id)` and
`async call_service(domain, service, data)`. The memory it keeps - what a
thermostat was set to before Ohmter touched it, when a `number` was last
written - is a plain dict so the integration can persist it and the watchdog
can read it.

Three rules from docs/HOME_ASSISTANT_BRIDGE.md are enforced here:

* An expired command is refused before anything is called.
* A thermostat window *restores what was there*, not "eco": the baseline is
  captured on the first set of a window and cleared on restore.
* A `number` entity is written at most once per MIN_NUMBER_WRITE_INTERVAL,
  because a charger behind Home Assistant may still be a Zaptec.
"""
from __future__ import annotations

import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from .protocol import ACTIONS_BY_KIND, MIN_NUMBER_WRITE_INTERVAL_SECONDS

CallService = Callable[[str, str, Dict[str, Any]], Awaitable[None]]
GetState = Callable[[str], Any]


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
    if action not in ACTIONS_BY_KIND.get(kind, ()):
        raise CommandRefused(f"{action} is not an action for a {kind}")
    expires = _parse(command.get("expires_at"))
    if expires is not None:
        a, b = _same_clock(now, expires)
        if a >= b:
            raise CommandRefused("expired before the bridge ran it")
    entities = device.get("entities") or {}
    params = command.get("params") or {}

    if kind == "ev":
        note = await _execute_ev(action, params, entities, get_state, call_service, memory, now)
    else:
        note = await _execute_thermostat(action, params, entities, get_state, call_service, memory)
    executed_ids.add(cid)
    memory["last_action"] = action
    memory["last_action_at"] = now.isoformat()
    return note


async def _execute_ev(action, params, entities, get_state, call_service, memory, now) -> str:
    switch = entities.get("switch")
    if action in ("on", "off"):
        if not switch:
            raise CommandRefused("no charging switch mapped")
        st = get_state(switch)
        if st is None or st.state in ("unavailable", "unknown"):
            raise CommandRefused(f"{switch} is unavailable - the charger's own mode may be in control")
        await call_service("switch", "turn_on" if action == "on" else "turn_off", {"entity_id": switch})
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
        last = _parse(memory.get("last_number_write_at"))
        if last is not None:
            a, b = _same_clock(now, last)
            if (a - b).total_seconds() < MIN_NUMBER_WRITE_INTERVAL_SECONDS:
                current = _num(st.state)
                if current is not None and abs(current - amps) < 0.5:
                    return "current already at value"
                raise CommandRefused("max current was written less than a minute ago")
        if memory.get("current_before") is None:
            memory["current_before"] = _num(st.state)
        await call_service("number", "set_value", {"entity_id": number, "value": amps})
        memory["last_number_write_at"] = now.isoformat()
        return f"number.set_value {number}={amps}"
    raise CommandRefused(f"unknown EV action {action}")


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
        if mode and mode in (attrs.get("hvac_modes") or []):
            await call_service("climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": mode})
            notes.append(f"hvac_mode={mode}")
        if target is not None:
            lo, hi = _num(attrs.get("min_temp")), _num(attrs.get("max_temp"))
            if lo is not None and target < lo:
                target = lo
            if hi is not None and target > hi:
                target = hi
            await call_service("climate", "set_temperature", {"entity_id": climate, "temperature": target})
            notes.append(f"temperature={target}")
        return "climate " + " ".join(notes)
    if action == "restore":
        before = memory.get("thermostat_before")
        if not before:
            return "nothing to restore"
        if before.get("hvac_mode") and before["hvac_mode"] in (attrs.get("hvac_modes") or [before["hvac_mode"]]):
            await call_service("climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": before["hvac_mode"]})
        if before.get("preset_mode") and before["preset_mode"] in (attrs.get("preset_modes") or []):
            await call_service("climate", "set_preset_mode", {"entity_id": climate, "preset_mode": before["preset_mode"]})
        if before.get("temperature") is not None:
            await call_service("climate", "set_temperature", {"entity_id": climate, "temperature": before["temperature"]})
        memory["thermostat_before"] = None
        return f"climate restored to {before}"
    raise CommandRefused(f"unknown thermostat action {action}")
