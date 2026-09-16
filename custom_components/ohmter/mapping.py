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
import hashlib
import json
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


def unit_factor(unit: str, target: str) -> float:
    """Multiplier from `unit` to W / kWh / A.

    Case matters exactly once: Home Assistant accepts both ``MW`` (megawatt)
    and ``mW`` (milliwatt) for power, and ``MWh`` and ``mWh`` for energy.
    Lower-casing first turned a 2.4 mW reading into 2.4 million watts. Every
    other prefix is taken case-insensitively, as integrations spell ``kW``
    more than one way.
    """
    raw = str(unit or "").strip()
    u = raw.lower()
    if target == "W":
        if raw == "mW":
            return 0.001
        return 1000.0 if u == "kw" else 1_000_000.0 if u == "mw" else 1.0
    if target == "kWh":
        if raw == "mWh":
            return 0.000001
        return 0.001 if u == "wh" else 1000.0 if u == "mwh" else 1.0
    if target == "A":
        return 0.001 if u == "ma" else 1.0
    return 1.0


def _to_target(value: float, unit: str, target: str) -> float:
    return value * unit_factor(unit, target)


def describe_entity(entity_id: str, state: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The facts about one entity that travel to Ohmter with the mapping.

    Only what names the entity and its kind of value: friendly name, domain,
    device_class, state_class, unit, the registry's unique_id and platform,
    the registry entry id and the Home Assistant device it belongs to. Never
    its state history. The registry entry id is what lets Ohmter tell a
    renamed entity_id (same entity, plans kept) from a different entity
    (plans held for review).
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
        "platform": str(meta.get("platform") or "")[:60],
        "registry_id": str(meta.get("registry_id") or "")[:80],
    }
    return {k: v for k, v in out.items() if v}


CONTROL_ROLE = {"ev": "switch", "thermostat": "climate"}


def same_entity(a_entity: Optional[str], a_meta: Optional[Dict[str, Any]],
                b_entity: Optional[str], b_meta: Optional[Dict[str, Any]]) -> bool:
    """The integration's copy of core/bridge.py `same_target`, on two picks.

    Registry entry id first, then platform + unique_id; without either on both
    sides only an unchanged entity_id counts.
    """
    if not a_entity and not b_entity:
        return True
    if not a_entity or not b_entity:
        return False
    a, b = a_meta or {}, b_meta or {}
    if a_entity.split(".", 1)[0] != b_entity.split(".", 1)[0]:
        return False
    if a.get("registry_id") and b.get("registry_id"):
        return a["registry_id"] == b["registry_id"]
    if a.get("unique_id") and b.get("unique_id") and a.get("platform") and b.get("platform"):
        return (a["platform"], a["unique_id"]) == (b["platform"], b["unique_id"])
    if a_entity != b_entity:
        return False
    if a.get("unique_id") and b.get("unique_id") and a["unique_id"] != b["unique_id"]:
        return False
    return True


def control_target_changed(prior: Optional[Dict[str, Any]], kind: str, entities: Dict[str, str],
                           meta: Dict[str, Any]) -> bool:
    """Whether an edit points control at a different entity than the saved mapping.

    A grant given for one switch is never carried to another: when this is
    True the confirm screen starts unticked and says which entity changed.
    """
    role = CONTROL_ROLE.get(kind)
    if not role or not prior:
        return False
    before = (prior.get("entities") or {}).get(role)
    after = (entities or {}).get(role)
    return not same_entity(before, (prior.get("meta") or {}).get(role), after, (meta or {}).get(role))


ACTION_ROLE = {"on": "switch", "off": "switch", "set_current": "current", "set_temperature": "climate", "restore": "climate"}


def replaced_entity(device: Dict[str, Any], action: str, live_registry_id: Optional[str]) -> Optional[str]:
    """The entity_id a command would touch if that entity was replaced since it was mapped, else None.

    An entity deleted in Home Assistant and made again can come back under the
    same entity_id with a new registry entry. The mapping still names the id,
    so without this check a command decided for the old entity - or the
    watchdog's release of it - would run on the new one. Only a registry id
    recorded at mapping time *and* a different live one count as proof;
    anything less is not treated as a replacement.
    """
    role = ACTION_ROLE.get(str(action or ""))
    entity_id = ((device or {}).get("entities") or {}).get(role) if role else None
    if not entity_id:
        return None
    stored = (((device or {}).get("meta") or {}).get(role) or {}).get("registry_id")
    if stored and live_registry_id and stored != live_registry_id:
        return entity_id
    return None


def mapping_rev(entities: Dict[str, Any]) -> str:
    """Fingerprint of a role -> entity map; identical to core/bridge.py `mapping_rev`."""
    items = sorted((str(k), str(v)) for k, v in (entities or {}).items() if v)
    return hashlib.sha256(json.dumps(items, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


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


# Every sentence the confirm screen builds in code, as (English, Dutch). Home
# Assistant translates strings.json but not description placeholders, so text
# assembled here has to carry both languages itself - otherwise a Dutch
# household reads English inside a Dutch dialog, which is what the first real
# Home Assistant run showed. Role labels match the per-kind `data` labels in
# strings.json; tests/test_ha_mapping_integration.py holds the two together.
KIND_LABELS = {
    "ev": ("EV charger", "laadpaal"),
    "thermostat": ("thermostat", "thermostaat"),
    "p1": ("P1 smart meter", "P1 slimme meter"),
    "solar": ("solar inverter", "omvormer"),
    "battery": ("home battery", "thuisbatterij"),
}
ROLE_LABELS = {
    "ev": {"switch": ("Charging switch", "Laadschakelaar"), "power": ("Charging power", "Laadvermogen"),
           "current": ("Maximum charge current", "Maximale laadstroom"), "energy": ("Charger energy", "Energie van de laadpaal"),
           "plugged_in": ("Car plugged in", "Auto aangesloten")},
    "thermostat": {"climate": ("Thermostat", "Thermostaat")},
    "p1": {"power": ("Net grid power", "Netto vermogen op de aansluiting"),
           "import_power": ("Import power (from the grid)", "Afgenomen vermogen (van het net)"),
           "export_power": ("Export power (to the grid)", "Teruggeleverd vermogen (aan het net)"),
           "import_energy": ("Total imported energy", "Totaal afgenomen energie"),
           "export_energy": ("Total exported energy", "Totaal teruggeleverde energie")},
    "solar": {"power": ("Solar production", "Opgewekt vermogen"), "consumption": ("House consumption", "Verbruik van het huis"),
              "grid_import": ("Grid import", "Afname van het net"), "grid_export": ("Grid export", "Teruglevering aan het net")},
    "battery": {"soc": ("State of charge", "Laadtoestand"), "power": ("Battery power", "Batterijvermogen")},
}
_TEXT = {
    "unavailable": ("unavailable", "niet beschikbaar"),
    "on": ("on", "aan"),
    "off": ("off", "uit"),
    "none": ("-", "-"),
    "temp": ("{cur} °C now, target {tgt} °C, {mode}", "{cur} °C nu, doel {tgt} °C, {mode}"),
    "monitoring": ("monitoring", "volgen"),
    "current_limit": ("current limit", "stroomlimiet"),
    "session_energy": ("session energy from the charger", "sessie-energie van de laadpaal"),
    "vehicle_connection": ("car plugged-in state", "aansluitstatus van de auto"),
    "control_once_confirmed": ("charging control: only once you confirm it below",
                               "laadbediening: pas nadat je hieronder bevestigt"),
    "control_none": ("charging control: not possible, no charging switch is picked",
                     "laadbediening: niet mogelijk, er is geen laadschakelaar gekozen"),
    "thermostat_features": ("reading temperatures; control: only once you confirm it below",
                            "temperaturen uitlezen; bediening: pas nadat je hieronder bevestigt"),
    "read_only": ("reading only - Ohmter never controls this kind of device",
                  "alleen uitlezen - Ohmter bedient dit soort apparaat nooit"),
    "on_device": ("on device {device}", "op apparaat {device}"),
    "changed": ("The control entity changes from {old} to {new}. The confirmation you gave for the old entity does not carry "
                "over: confirm again for the new one. Plans saved in Ohmter for the old entity will not run until you "
                "enable them again in the Ohmter dashboard.",
                "De bedieningsentiteit wijzigt van {old} naar {new}. Je bevestiging voor de oude entiteit geldt niet voor de "
                "nieuwe: bevestig opnieuw. Plannen die in Ohmter voor de oude entiteit zijn opgeslagen, lopen pas weer als je "
                "ze in het Ohmter-dashboard opnieuw inschakelt."),
    "split_devices": ("Check this: the charging switch belongs to {a} and the charging power to {b}. Ohmter confirms that "
                      "charging started by watching the power, so both should be the same charger.",
                      "Controleer dit: de laadschakelaar hoort bij {a} en het laadvermogen bij {b}. Ohmter ziet aan het "
                      "vermogen of het laden echt begon, dus beide horen bij dezelfde laadpaal."),
}


_REFUSALS = {
    "wrong_unit": ("an entity reports a unit Ohmter cannot read for its role",
                   "een entiteit meldt een eenheid die Ohmter voor die rol niet kan lezen"),
    "wrong_domain": ("an entity is the wrong kind for its role", "een entiteit is van het verkeerde soort voor die rol"),
    "missing_role": ("a required entity is missing", "een verplichte entiteit ontbreekt"),
    "same_entity_twice": ("one entity is picked for two roles", "één entiteit is voor twee rollen gekozen"),
    "bad_name": ("a device name is too short", "een apparaatnaam is te kort"),
    "duplicate_device_key": ("two devices share one key", "twee apparaten delen één sleutel"),
    "": ("the change did not pass Ohmter's checks", "de wijziging kwam niet door de controle van Ohmter"),
}


def refusal_reason(code: Optional[str], status: Optional[int], lang: int) -> str:
    """A sentence for the repair issue - never the server's raw response body."""
    return _REFUSALS.get(str(code or ""), _REFUSALS[""])[lang]


def lang_index(language: Optional[str]) -> int:
    """0 for English, 1 for Dutch - Home Assistant's configured language decides."""
    return 1 if str(language or "").lower().startswith("nl") else 0


def text(key: str, lang: int, **values: Any) -> str:
    return _TEXT[key][lang].format(**values) if values else _TEXT[key][lang]


def kind_label(kind: str, lang: int) -> str:
    return KIND_LABELS.get(kind, (kind, kind))[lang]


def role_label(kind: str, role: str, lang: int) -> str:
    return ROLE_LABELS.get(kind, {}).get(role, (role.replace("_", " "),) * 2)[lang]


def format_value(role_check: Dict[str, Any], lang: int = 0) -> str:
    """A short, unit-carrying rendering for a confirm screen. Never a zero for nothing."""
    p = role_check.get("problem")
    if not role_check.get("entity_id"):
        return "-"
    if p == "unavailable":
        return text("unavailable", lang)
    if p:
        return p.replace("_", " ")
    v = role_check.get("value")
    unit = role_check.get("unit") or ""
    if isinstance(v, bool):
        return text("on" if v else "off", lang)
    if isinstance(v, dict):
        cur, tgt = v.get("current_temp_c"), v.get("target_temp_c")
        return text("temp", lang, cur=cur if cur is not None else "?", tgt=tgt if tgt is not None else "?", mode=v.get("mode"))
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


def _entity_line(entity_id: str, meta: Dict[str, Any], device_name: Optional[str], lang: int) -> str:
    name = (meta or {}).get("name") or entity_id
    line = f"{name} ({entity_id})" if name != entity_id else entity_id
    if device_name:
        line += " · " + text("on_device", lang, device=device_name)
    return line


def confirm_placeholders(
    kind: str,
    pv: Dict[str, Any],
    draft: Dict[str, Any],
    *,
    lang: int,
    prior: Optional[Dict[str, Any]] = None,
    device_names: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Everything the check-before-saving screen fills in, in one language.

    `prior` is the saved mapping when editing; `device_names` maps a role to
    the Home Assistant device its entity belongs to, from the registry.
    """
    device_names = device_names or {}
    meta = draft.get("meta") or {}
    entities = draft.get("entities") or {}
    lines = []
    for r in pv["roles"]:
        if not r["entity_id"]:
            if r["required"]:
                lines.append(f"- {role_label(kind, r['role'], lang)}: -")
            continue
        lines.append(f"- {role_label(kind, r['role'], lang)}: {format_value(r, lang)} ({r['entity_id']})")
    feats = pv["features"]
    control_role = CONTROL_ROLE.get(kind)
    can_control = bool(control_role and entities.get(control_role))
    if kind == "ev":
        parts = [text(k, lang) for k in ("monitoring", "current_limit", "session_energy", "vehicle_connection") if feats.get(k)]
        features = ", ".join(parts or ["-"]) + "; " + text("control_once_confirmed" if can_control else "control_none", lang)
    elif kind == "thermostat":
        features = text("thermostat_features", lang)
    else:
        features = text("read_only", lang)
    quiet = ", ".join(role_label(kind, r["role"], lang) for r in pv["warnings"]) or "-"
    control_entity = "-"
    if can_control:
        eid = entities[control_role]
        control_entity = _entity_line(eid, meta.get(control_role) or {}, device_names.get(control_role), lang)
    notices = []
    if can_control and control_target_changed(prior, kind, entities, meta):
        old_eid = ((prior or {}).get("entities") or {}).get(control_role)
        old = _entity_line(old_eid, ((prior or {}).get("meta") or {}).get(control_role) or {}, None, lang) if old_eid else "-"
        notices.append(text("changed", lang, old=old, new=_entity_line(entities[control_role], meta.get(control_role) or {}, None, lang)))
    if kind == "ev" and entities.get("switch") and entities.get("power"):
        a = (meta.get("switch") or {}).get("ha_device_id")
        b = (meta.get("power") or {}).get("ha_device_id")
        if a and b and a != b:
            notices.append(text("split_devices", lang, a=device_names.get("switch") or a, b=device_names.get("power") or b))
    return {
        "name": str(draft.get("name") or ""),
        "kind": kind_label(kind, lang),
        "readings": "\n".join(lines) or "-",
        "features": features,
        "quiet": quiet,
        "control_entity": control_entity,
        "notice": "\n\n".join(notices),
    }


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
