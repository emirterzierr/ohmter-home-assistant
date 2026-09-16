"""Pairing (config flow) and mapping entities to Ohmter devices (options flow).

Pairing asks for one thing: the code shown in the Ohmter dashboard. No Ohmter
password, no Home Assistant token, no vendor credential.

Mapping is three screens, and the middle one is the point. The household
picks entities per role (filtered by domain and device class, each field
saying what it is for and what is lost without it); the integration then
*reads* those entities and shows what came back - "Power: 2,400 W", "Energy:
unavailable", "Charging control: not configured" - and only then asks them to
confirm. Control is a grant made on that screen in so many words; a switch
that is merely picked is watched, never switched. Nothing is called on any
entity during mapping.

The entity picker is a step per kind (`map_ev`, `map_solar`, ...), because
its labels are: one shared step labelled a solar inverter's production
"Charging power - what the charger draws now".

A grant belongs to one entity. When an edit points control at a different
entity than the saved mapping - by registry identity, not by name - the
confirmation starts unticked and the screen says what changed; Ohmter then
holds the plans made for the old entity for review.

A device keeps its `device_key` for life. Renaming changes the label Ohmter
shows, not the identity its history hangs on.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_BASE_URL,
    CONF_BRIDGE_ID,
    CONF_CODE,
    CONF_NAME,
    CONF_TOKEN,
    DOMAIN,
    MAX_NAME_LENGTH,
    OPT_DEVICES,
    VERSION,
)
from .coordinator import make_transport
from .mapping import (
    CONTROL_ROLE,
    confirm_placeholders,
    control_target_changed,
    describe_entity,
    kind_label,
    lang_index,
    preview,
)
from .ohmter_client import BadPairingCode, OhmterClient, OhmterError, OhmterUnreachable
from .protocol import DEFAULT_BASE_URL, DEVICE_KINDS, REQUIRED_ROLES, ROLES_BY_KIND

# Per role: which entities the picker offers.
_ROLE_SELECTORS: Dict[str, Dict[str, selector.EntitySelectorConfig]] = {
    "ev": {
        "switch": selector.EntitySelectorConfig(domain="switch"),
        "power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "current": selector.EntitySelectorConfig(domain="number"),
        "energy": selector.EntitySelectorConfig(domain="sensor", device_class="energy"),
        "plugged_in": selector.EntitySelectorConfig(domain="binary_sensor"),
    },
    "thermostat": {
        "climate": selector.EntitySelectorConfig(domain="climate"),
    },
    "p1": {
        "power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "import_power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "export_power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "import_energy": selector.EntitySelectorConfig(domain="sensor", device_class="energy"),
        "export_energy": selector.EntitySelectorConfig(domain="sensor", device_class="energy"),
    },
    "solar": {
        "power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "consumption": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "grid_import": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
        "grid_export": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
    },
    "battery": {
        "soc": selector.EntitySelectorConfig(domain="sensor", device_class="battery"),
        "power": selector.EntitySelectorConfig(domain="sensor", device_class="power"),
    },
}

# The role whose Home Assistant device stands for the physical device.
_IDENTITY_ROLE = {"ev": "power", "thermostat": "climate", "p1": "power", "solar": "power", "battery": "soc"}


async def _pair(hass, base_url: str, code: str, name: str) -> Dict[str, Any]:
    client = OhmterClient(base_url, None, make_transport(async_get_clientsession(hass)), user_agent=f"OhmterHA/{VERSION}")
    return await client.pair(
        code, name=name, ha_version=HA_VERSION, integration_version=VERSION,
        instance_id=await async_get_instance_id(hass),
    )


def _pair_error(exc: Exception) -> Dict[str, str]:
    """A wrong or expired code is the code field's error; the rest is the form's."""
    if isinstance(exc, BadPairingCode):
        return {CONF_CODE: "code_expired" if exc.code == "code_expired" else "bad_code"}
    if isinstance(exc, OhmterUnreachable):
        return {"base": "cannot_connect"}
    return {"base": "unknown"}


class OhmterConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
            name = (user_input.get(CONF_NAME) or "Home Assistant").strip()
            try:
                body = await _pair(self.hass, base_url, user_input[CONF_CODE], name)
            except OhmterError as exc:
                errors = _pair_error(exc)
            else:
                await self.async_set_unique_id(body["bridge_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=body.get("name") or name,
                    data={CONF_BASE_URL: base_url, CONF_TOKEN: body["bridge_token"], CONF_BRIDGE_ID: body["bridge_id"]},
                    options={OPT_DEVICES: []},
                )
        schema = vol.Schema({
            vol.Required(CONF_CODE): str,
            vol.Optional(CONF_NAME, default="Home Assistant"): str,
        })
        # The server field exists for development and for the end-to-end run
        # against a local Ohmter (scripts/ha_e2e.py sets OHMTER_BASE_URL in the
        # container); a household never sees it.
        if os.environ.get("OHMTER_BASE_URL"):
            schema = schema.extend({vol.Optional(CONF_BASE_URL, default=os.environ["OHMTER_BASE_URL"]): str})
        # What was typed stays in the form after an error, so a typo can be
        # seen and corrected rather than retyped from memory.
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}), errors=errors,
        )

    async def async_step_reauth(self, entry_data: Dict[str, Any]) -> ConfigFlowResult:
        """Ohmter says this bridge was disconnected: ask for a fresh code."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                body = await _pair(self.hass, entry.data[CONF_BASE_URL], user_input[CONF_CODE], entry.title)
            except OhmterError as exc:
                errors = _pair_error(exc)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: body["bridge_token"], CONF_BRIDGE_ID: body["bridge_id"]},
                )
        schema = vol.Schema({vol.Required(CONF_CODE): str})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "OhmterOptionsFlow":
        return OhmterOptionsFlow()


class OhmterOptionsFlow(OptionsFlow):
    """Add, edit or remove the devices this Home Assistant reports to Ohmter."""

    def __init__(self) -> None:
        self._kind: Optional[str] = None
        self._editing_key: Optional[str] = None
        self._prior: Optional[Dict[str, Any]] = None
        self._draft: Optional[Dict[str, Any]] = None
        self._preview: Optional[Dict[str, Any]] = None
        self._device_names: Dict[str, str] = {}

    @property
    def _lang(self) -> int:
        return lang_index(self.hass.config.language)

    def _devices(self) -> List[Dict[str, Any]]:
        return list(self.config_entry.options.get(OPT_DEVICES) or [])

    def _get_state(self, entity_id: str):
        return self.hass.states.get(entity_id) if entity_id else None

    def _registry_meta(self, entity_id: str) -> Dict[str, Any]:
        reg = er.async_get(self.hass).async_get(entity_id)
        if reg is None:
            return {}
        # The registry entry id survives an entity_id rename and is new for an
        # entity deleted and made again - the proof Ohmter uses to keep plans
        # through a rename and to hold them when the target really changed.
        out: Dict[str, Any] = {"unique_id": reg.unique_id, "device_id": reg.device_id, "platform": reg.platform,
                               "registry_id": reg.id}
        if reg.device_id:
            dev = dr.async_get(self.hass).async_get(reg.device_id)
            if dev is not None:
                out["device_name"] = dev.name_by_user or dev.name
        if reg.original_device_class and not out.get("device_class"):
            out["device_class"] = reg.original_device_class
        return out

    def _device_option(self, d: Dict[str, Any]) -> selector.SelectOptionDict:
        return selector.SelectOptionDict(value=d["device_key"], label=f"{d['name']} ({kind_label(d['kind'], self._lang)})")

    # -- menu ---------------------------------------------------------------

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        options = ["add_device"]
        if self._devices():
            options.extend(["edit_device", "remove_device"])
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_device(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            self._kind = user_input["kind"]
            self._editing_key = None
            self._prior = None
            self._draft = None
            return await self._map_step(None)
        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema({
                vol.Required("kind"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(DEVICE_KINDS), translation_key="kind", mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_edit_device(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        devices = self._devices()
        if user_input is not None:
            key = user_input["device"]
            current = next((d for d in devices if d["device_key"] == key), None)
            if current is None:
                return self.async_abort(reason="device_gone")
            self._kind = current["kind"]
            self._editing_key = key
            self._prior = dict(current)
            self._draft = dict(current)
            return await self._map_step(None)
        return self.async_show_form(
            step_id="edit_device",
            data_schema=vol.Schema({
                vol.Required("device"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[self._device_option(d) for d in devices],
                                                  mode=selector.SelectSelectorMode.LIST)
                ),
            }),
        )

    # -- pick ---------------------------------------------------------------

    async def async_step_map_entities(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        # Kept so an older client driving the flow by this step id still lands in the right form.
        return await self._map_step(user_input)

    async def async_step_map_ev(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._map_step(user_input)

    async def async_step_map_thermostat(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._map_step(user_input)

    async def async_step_map_p1(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._map_step(user_input)

    async def async_step_map_solar(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._map_step(user_input)

    async def async_step_map_battery(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._map_step(user_input)

    async def _map_step(self, user_input: Optional[Dict[str, Any]]) -> ConfigFlowResult:
        kind = self._kind or "ev"
        errors: Dict[str, str] = {}
        prior = self._draft or {}
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            entities = {role: user_input[role] for role in ROLES_BY_KIND[kind] if user_input.get(role)}
            if len(name) < 2:
                errors["name"] = "name_too_short"
            elif len(name) > MAX_NAME_LENGTH:
                # Ohmter stores at most 80 characters; a longer name used to be
                # accepted here and refused by the server afterwards.
                errors["name"] = "name_too_long"
            else:
                taken = any(d["name"].strip().lower() == name.lower() and d["device_key"] != self._editing_key
                            for d in self._devices())
                if taken:
                    errors["name"] = "name_taken"
            # Read what the household picked. Nothing is called; a wrong
            # domain or unit stops here with the field named, an entity
            # that is merely quiet is shown as such on the next screen.
            pv = preview(kind, entities, self._get_state, now=dt_util.utcnow())
            for r in pv["blocking"]:
                field = r["role"] if r["role"] in ROLES_BY_KIND[kind] else "base"
                errors[field] = {"missing": "missing_role", "wrong_domain": "wrong_domain", "wrong_unit": "wrong_unit",
                                 "not_numeric": "not_numeric", "same_entity": "same_entity_twice"}.get(r["problem"], "invalid_entity")
            if not errors:
                meta = {}
                self._device_names = {}
                for role, eid in entities.items():
                    reg = self._registry_meta(eid)
                    meta[role] = describe_entity(eid, self._get_state(eid), reg)
                    if reg.get("device_name"):
                        self._device_names[role] = str(reg["device_name"])
                identity_role = _IDENTITY_ROLE.get(kind)
                ha_device_id = (meta.get(identity_role) or {}).get("ha_device_id") or ""
                self._draft = {
                    **prior,
                    "kind": kind, "name": name, "entities": entities, "meta": meta, "ha_device_id": ha_device_id,
                    "device_key": self._editing_key or prior.get("device_key") or self._new_key(name, kind),
                }
                self._preview = pv
                return await self.async_step_confirm_mapping()
        defaults = user_input or prior
        fields: Dict[Any, Any] = {vol.Required("name", default=defaults.get("name", "")): str}
        prior_entities = (defaults.get("entities") if isinstance(defaults.get("entities"), dict) else None) or defaults
        for role in ROLES_BY_KIND[kind]:
            marker = vol.Required if role in REQUIRED_ROLES[kind] else vol.Optional
            current = prior_entities.get(role)
            key = marker(role, default=current) if current else marker(role)
            fields[key] = selector.EntitySelector(_ROLE_SELECTORS[kind][role])
        return self.async_show_form(step_id=f"map_{kind}", data_schema=vol.Schema(fields), errors=errors)

    def _new_key(self, name: str, kind: str) -> str:
        devices = self._devices()
        base = slugify(name)[:40] or f"{kind}-{len(devices) + 1}"
        key, n = base, 2
        while any(d["device_key"] == key for d in devices):
            key = f"{base[:36]}-{n}"
            n += 1
        return key

    # -- check, then confirm ---------------------------------------------------

    async def async_step_confirm_mapping(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        draft = self._draft
        pv = self._preview
        if not draft or not pv:
            return await self.async_step_init()
        kind = draft["kind"]
        control_role = CONTROL_ROLE.get(kind)
        can_control = bool(control_role and draft["entities"].get(control_role))
        # A grant is for one entity. Pointing control somewhere else starts
        # unticked; only an unchanged target keeps what was confirmed.
        target_changed = control_target_changed(self._prior, kind, draft["entities"], draft.get("meta") or {})
        # The grant is worded per kind: a thermostat is not "switched on to charge".
        grant_field = "confirm_control" if kind == "ev" else "confirm_thermostat_control"
        if user_input is not None:
            confirmed = bool(user_input.get(grant_field)) if can_control else False
            device = {
                "device_key": draft["device_key"], "kind": kind, "name": draft["name"],
                "entities": draft["entities"], "meta": draft["meta"], "ha_device_id": draft.get("ha_device_id") or "",
                "control_confirmed": confirmed,
            }
            devices = self._devices()
            if self._editing_key:
                devices = [device if d["device_key"] == self._editing_key else d for d in devices]
            else:
                devices.append(device)
            return self.async_create_entry(data={OPT_DEVICES: devices})
        schema: Dict[Any, Any] = {}
        if can_control:
            default = False if target_changed else bool(draft.get("control_confirmed", False))
            schema[vol.Optional(grant_field, default=default)] = selector.BooleanSelector()
        return self.async_show_form(
            step_id="confirm_mapping",
            data_schema=vol.Schema(schema),
            description_placeholders=confirm_placeholders(
                kind, pv, draft, lang=self._lang, prior=self._prior, device_names=self._device_names,
            ),
        )

    # -- remove ---------------------------------------------------------------

    async def async_step_remove_device(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        devices = self._devices()
        if user_input is not None:
            drop = set(user_input.get("devices") or [])
            return self.async_create_entry(data={OPT_DEVICES: [d for d in devices if d["device_key"] not in drop]})
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema({
                vol.Required("devices"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[self._device_option(d) for d in devices], multiple=True,
                                                  mode=selector.SelectSelectorMode.LIST)
                ),
            }),
        )
