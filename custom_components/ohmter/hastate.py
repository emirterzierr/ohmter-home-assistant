"""Turn Home Assistant entity states into the kind-shaped report Ohmter expects.

Pure: `get_state(entity_id)` is injected and returns anything with `.state`
and `.attributes` (Home Assistant's `State`, or a stub in tests). Units are
normalised here so the server only ever sees W, kWh, A, °C and %. An entity
that is `unavailable` or `unknown` makes the whole device `available: False`
when it is a required role, and is simply omitted when it is optional - a
charger whose energy sensor dropped out is still a charger.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .mapping import mapping_rev, unit_factor
from .protocol import REQUIRED_ROLES

_BAD_STATES = ("unavailable", "unknown", "", None)


def _num(state: Any, *, unit_from: Optional[str] = None, to: Optional[str] = None) -> Optional[float]:
    if state is None or state.state in _BAD_STATES:
        return None
    try:
        v = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = str((state.attributes or {}).get("unit_of_measurement") or unit_from or "").strip()
    if to in ("W", "kWh", "A"):
        # Case-aware: mW is a milliwatt and MW a megawatt (mapping.unit_factor).
        v *= unit_factor(unit, to)
    return v


def _bool(state: Any) -> Optional[bool]:
    if state is None or state.state in _BAD_STATES:
        return None
    return str(state.state).lower() in ("on", "true", "open", "plugged", "connected", "charging")


def _available(kind: str, entities: Dict[str, str], get_state: Callable[[str], Any]) -> bool:
    for role in REQUIRED_ROLES.get(kind, ()):
        st = get_state(entities.get(role, ""))
        if st is None or st.state in _BAD_STATES:
            return False
    if kind == "p1" and "power" not in entities:
        for role in ("import_power", "export_power"):
            st = get_state(entities.get(role, ""))
            if st is None or st.state in _BAD_STATES:
                return False
    return True


def _unavailable_roles(ent: Dict[str, str], get_state: Callable[[str], Any]) -> list:
    out = []
    for role, eid in ent.items():
        st = get_state(eid)
        if st is None or st.state in _BAD_STATES:
            out.append(role)
    return out


def _latest_update(ent: Dict[str, str], get_state: Callable[[str], Any]) -> Optional[str]:
    """The newest `last_updated` among the mapped entities, as ISO, or None."""
    best = None
    for eid in ent.values():
        st = get_state(eid)
        lu = getattr(st, "last_updated", None) if st is not None else None
        if lu is None:
            continue
        try:
            iso = lu.isoformat()
        except AttributeError:
            iso = str(lu)
        if best is None or iso > best:
            best = iso
    return best


def read_device(kind: str, entities: Dict[str, str], get_state: Callable[[str], Any]) -> Dict[str, Any]:
    """The `state` object for one device in a `/bridge/sync` report."""
    ent = {k: v for k, v in (entities or {}).items() if v}
    st: Dict[str, Any] = {"available": _available(kind, ent, get_state)}
    # Which mapped entities are silent right now, and when any of them last
    # changed - so the dashboard can say "energy sensor unavailable" beside a
    # charger that is otherwise reporting, and "fresh" only with evidence.
    unavailable = _unavailable_roles(ent, get_state)
    if unavailable:
        st["unavailable_roles"] = unavailable
    updated = _latest_update(ent, get_state)
    if updated:
        st["updated_at"] = updated
    if not st["available"]:
        return st
    if kind == "ev":
        # The switch is optional now: a charger with none is monitored only.
        sw = get_state(ent["switch"]) if "switch" in ent else None
        st["switch_on"] = _bool(sw) if sw is not None else None
        st["power_w"] = _num(get_state(ent["power"]), to="W")
        if "current" in ent:
            cur = get_state(ent["current"])
            st["max_current_a"] = _num(cur, to="A")
            attrs = (cur.attributes or {}) if cur is not None else {}
            if attrs.get("max") is not None:
                st["current_limit_max_a"] = _num_attr(attrs.get("max"))
        if "energy" in ent:
            st["session_energy_kwh"] = _num(get_state(ent["energy"]), to="kWh")
        if "plugged_in" in ent:
            st["plugged_in"] = _bool(get_state(ent["plugged_in"]))
        power = st.get("power_w") or 0.0
        # Without a switch, drawing power *is* charging; with one, both.
        st["charging"] = power > 100.0 and (st.get("switch_on") is not False)
        # The vendor's own smart mode shows up as the switch going unavailable
        # (Wallbox eco-smart) - reported so the tile can say who is in charge.
        if "switch" in ent:
            st["vendor_mode"] = None if sw is not None and sw.state not in _BAD_STATES else "switch_unavailable"
        else:
            st["vendor_mode"] = None
    elif kind == "thermostat":
        cl = get_state(ent["climate"])
        attrs = cl.attributes or {}
        st["hvac_mode"] = str(cl.state)
        st["current_temp_c"] = _num_attr(attrs.get("current_temperature"))
        st["target_temp_c"] = _num_attr(attrs.get("temperature"))
        st["hvac_action"] = attrs.get("hvac_action")
        st["preset"] = attrs.get("preset_mode")
        st["min_temp_c"] = _num_attr(attrs.get("min_temp"))
        st["max_temp_c"] = _num_attr(attrs.get("max_temp"))
    elif kind == "p1":
        if "power" in ent:
            st["active_power_w"] = _num(get_state(ent["power"]), to="W")
        else:
            st["import_power_w"] = _num(get_state(ent["import_power"]), to="W")
            st["export_power_w"] = _num(get_state(ent["export_power"]), to="W")
        if "import_energy" in ent:
            st["total_import_kwh"] = _num(get_state(ent["import_energy"]), to="kWh")
        if "export_energy" in ent:
            st["total_export_kwh"] = _num(get_state(ent["export_energy"]), to="kWh")
    elif kind == "solar":
        st["production_w"] = _num(get_state(ent["power"]), to="W")
        for role, key in (("consumption", "consumption_w"), ("grid_import", "grid_import_w"), ("grid_export", "grid_export_w")):
            if role in ent:
                st[key] = _num(get_state(ent[role]), to="W")
    elif kind == "battery":
        st["soc_pct"] = _num(get_state(ent["soc"]))
        if "power" in ent:
            st["power_w"] = _num(get_state(ent["power"]), to="W")
    return st


def _num_attr(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_reports(devices: list, get_state: Callable[[str], Any], observed_at: str) -> list:
    out = []
    for d in devices:
        try:
            state = read_device(d["kind"], d.get("entities") or {}, get_state)
        except Exception as exc:  # noqa: BLE001 - one broken mapping must not stop the others
            state = {"available": False, "error": str(exc)[:120]}
        # Which mapping these values were read through. Ohmter ignores a report
        # whose mapping it has already replaced, so a sync that was in flight
        # during an Edit cannot be taken as the new entity's state.
        out.append({"device_key": d["device_key"], "state": state, "observed_at": observed_at,
                    "mapping_rev": mapping_rev(d.get("entities") or {})})
    return out
