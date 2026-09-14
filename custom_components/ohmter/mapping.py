"""Check a mapping before it is saved, and describe what it gives the household.

Pure: `get_state(entity_id)` returns anything with `.state`, `.attributes` and
optionally `.last_updated`; `get_meta(entity_id)` returns the registry facts
the integration looked up (unique_id, device_id, device name) or `{}`. Nothing
here calls a service. A preview *reads* the entities the household picked and
says, per role, what came back and whether it fits - so the confirm screen
can show "Power: 2,400 W - reading received" and "Charging control: not
configured" before Ohmter ever hears about the device.

Problem codes are stable strings the strings.json errors map to:

* `missing`        - a required role has no entity
* `wrong_domain`   - the entity is not from the domain this role needs
* `wrong_unit`     - the entity reports a unit Ohmter cannot read as this role
* `not_numeric`    - a numeric role's state is not a number
* `unavailable`    - the entity exists but is unavailable / unknown right now
* `same_entity`    - one entity picked for two roles
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, List, Optional

from .protocol import REQUIRED_ROLES, ROLE_DOMAINS, ROLE_UNITS, ROLES_BY_KIND

_BAD = ("unavailable", "unknown", "", None)
_NUMERIC_ROLES = tuple(ROLE_UNITS.keys())
_BOOL_ROLES = ("switch", "plugged_in")

# The feature each role unlocks, so the summary can say what a missing role costs.
FEATURE_BY_ROLE = {
    "ev": {"power": "monitoring", "switch": "control", "current": "current_limit",
           "energy": "session_energy", "plugged_in": "vehicle_connection"},
    "thermostat": {"climate": "control"},
    "p1": {"power": "monitoring", "import_power": "monitoring", "export_power": "monitoring",
           "import_energy": "energy_totals", "export_energy": "energy_totals"},
    "solar": {"power": "monitoring", "consumption": "self_use", "grid_import": "grid_flow", "grid_export": "grid_flow"},
    "battery": {"soc": "monitoring", "power": "battery_flow"},
}

_TARGET_UNIT = {
    "power": "W", "import_power": "W", "export_power": "W", "consumption": "W", "grid_import": "W", "grid_export": "W",
    "energy": "kWh", "import_energy": "kWh", "export_energy": "kWh", "current": "A", "soc": "%",
}


def _to_target(value: float, unit: str, target: str) -> float:
    u = unit.lower()
    if target == "W":
        return value * 1000.0 if u == "kw" else value * 1_000_000.0 if u == "mw" else value
    if target == "kWh":
        return value / 1000.0 if u == "wh" else value * 1000.0 if u == "mwh" else value
    if target == "A":
        return value / 1000.0 if u == "ma" else value
    return value


def describe_entity(entity_id: str, state: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The facts about one entity that travel to Ohmter with the mapping.

    Only what names the entity and its kind of value: friendly name, domain,
    device_class, state_class, unit, the registry's unique_id and the Home
    Assistant device it belongs to. Never its state history.
    """
    attrs = (getattr(state, "attributes", None) or {}) if state is not None else {}
    meta = meta or {}
    out = {
        "name": str(attrs.get("friendly_name") or meta.get("name") or entity_id)[:120],
        "domain": entity_id.split(".", 1)[0] if "." in entity_id else "",
        "device_class": str(attrs.get("device_class") or meta.get("device_class") or "")[:40],
        "state_class": str(attrs.get("state_class") or "")[:40],
        "unit": str(attrs.get("unit_of_measurement") or "")[:20],
        "unique_id": str(meta.get("unique_id") or "")[:120],
        "ha_device_id": str(meta.get("device_id") or "")[:80],
    }
    return {k: v for k, v in out.items() if v}


def check_role(kind: str, role: str, entity_id: Optional[str], state: Any, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """One role: does the picked entity fit, and what does it read right now."""
    required = role in REQUIRED_ROLES.get(kind, ())
    out: Dict[str, Any] = {
        "role": role, "required": required, "entity_id": entity_id or None, "problem": None,
        "value": None, "unit": _TARGET_UNIT.get(role), "raw_unit": None, "available": None,
        "last_updated": None, "age_seconds": None, "feature": FEATURE_BY_ROLE.get(kind, {}).get(role),
    }
    if not entity_id:
        out["problem"] = "missing" if required else None
        return out
    domain = entity_id.split(".", 1)[0]
    if domain not in ROLE_DOMAINS.get(role, (domain,)):
        out["problem"] = "wrong_domain"
        return out
    if state is None:
        out["available"] = False
        out["problem"] = "unavailable"
        return out
    attrs = getattr(state, "attributes", None) or {}
    raw_unit = str(attrs.get("unit_of_measurement") or "").strip()
    out["raw_unit"] = raw_unit or None
    if role in _NUMERIC_ROLES and raw_unit and raw_unit.lower() not in ROLE_UNITS[role]:
        out["problem"] = "wrong_unit"
        return out
    lu = getattr(state, "last_updated", None)
    if lu is not None:
        try:
            out["last_updated"] = lu.isoformat()
            if now is not None:
                a, b = now, lu
                if (a.tzinfo is None) != (b.tzinfo is None):
                    a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
                out["age_seconds"] = max(0, int((a - b).total_seconds()))
        except Exception:  # noqa: BLE001
            pass
    raw = getattr(state, "state", None)
    if raw in _BAD:
        out["available"] = False
        out["problem"] = "unavailable"
        return out
    out["available"] = True
    if role in _NUMERIC_ROLES:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            out["problem"] = "not_numeric"
            return out
        if v != v or v in (float("inf"), float("-inf")):
            out["problem"] = "not_numeric"
            return out
        out["value"] = _to_target(v, raw_unit, _TARGET_UNIT[role])
    elif role in _BOOL_ROLES:
        out["value"] = str(raw).lower() in ("on", "true", "open", "plugged", "connected", "charging")
    elif role == "climate":
        out["value"] = {
            "mode": str(raw), "current_temp_c": attrs.get("current_temperature"),
            "target_temp_c": attrs.get("temperature"), "hvac_modes": list(attrs.get("hvac_modes") or []),
        }
    return out


def preview(
    kind: str,
    entities: Dict[str, str],
    get_state: Callable[[str], Any],
    *,
    control_confirmed: bool = False,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Every role of `kind`, checked; the features the mapping unlocks; what blocks saving."""
    ent = {k: v for k, v in (entities or {}).items() if v}
    roles: List[Dict[str, Any]] = []
    for role in ROLES_BY_KIND.get(kind, ()):
        eid = ent.get(role)
        roles.append(check_role(kind, role, eid, get_state(eid) if eid else None, now))
    if kind == "p1" and "power" not in ent and not ("import_power" in ent and "export_power" in ent):
        for r in roles:
            if r["role"] == "power":
                r["required"] = True
                r["problem"] = "missing"
    if len(set(ent.values())) != len(ent):
        for r in roles:
            if r["entity_id"] and list(ent.values()).count(r["entity_id"]) > 1:
                r["problem"] = "same_entity"
    # What blocks saving is a wrong entity, not a quiet one: an entity that is
    # unavailable right now may be back in a minute, and the device shows
    # "no recent reading" until then. Wrong domain or unit never fixes itself.
    blocking = [r for r in roles if r["problem"] in ("missing", "wrong_domain", "wrong_unit", "not_numeric", "same_entity")]
    warnings = [r for r in roles if r["problem"] == "unavailable"]
    features = features_for(kind, ent, control_confirmed)
    return {"kind": kind, "roles": roles, "blocking": blocking, "warnings": warnings, "features": features,
            "control_confirmed": bool(control_confirmed)}


def features_for(kind: str, entities: Dict[str, str], control_confirmed: bool) -> Dict[str, bool]:
    ent = {k: v for k, v in (entities or {}).items() if v}
    f = {"monitoring": False, "control": False, "current_limit": False, "session_energy": False,
         "vehicle_connection": False, "scheduling": False}
    if kind == "ev":
        f["monitoring"] = "power" in ent
        f["control"] = bool(control_confirmed) and "switch" in ent
        f["current_limit"] = "current" in ent
        f["session_energy"] = "energy" in ent
        f["vehicle_connection"] = "plugged_in" in ent
        f["scheduling"] = f["control"]
    elif kind == "thermostat":
        f["monitoring"] = "climate" in ent
        f["control"] = bool(control_confirmed) and "climate" in ent
        f["scheduling"] = f["control"]
    else:
        f["monitoring"] = bool(ent)
    return f


def format_value(role_check: Dict[str, Any]) -> str:
    """A short, unit-carrying rendering for a confirm screen. Never a zero for nothing."""
    p = role_check.get("problem")
    if not role_check.get("entity_id"):
        return "-"
    if p == "unavailable":
        return "unavailable"
    if p:
        return p.replace("_", " ")
    v = role_check.get("value")
    unit = role_check.get("unit") or ""
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, dict):
        cur, tgt = v.get("current_temp_c"), v.get("target_temp_c")
        return f"{cur if cur is not None else '?'} °C now, target {tgt if tgt is not None else '?'} °C, {v.get('mode')}"
    if isinstance(v, (int, float)):
        if unit == "W":
            return f"{v:,.0f} W"
        if unit == "kWh":
            return f"{v:,.1f} kWh"
        if unit == "A":
            return f"{v:g} A"
        if unit == "%":
            return f"{v:g} %"
        return f"{v:g}"
    return "-"


def spec_for_server(device: Dict[str, Any]) -> Dict[str, Any]:
    """The device as `/bridge/devices` receives it: entities, their facts, the grant."""
    return {
        "device_key": device["device_key"],
        "kind": device["kind"],
        "name": device["name"],
        "entities": dict(device.get("entities") or {}),
        "meta": dict(device.get("meta") or {}),
        "control_confirmed": bool(device.get("control_confirmed", False)),
        "ha_device_id": str(device.get("ha_device_id") or ""),
    }
