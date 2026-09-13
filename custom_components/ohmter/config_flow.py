"""Pairing (config flow) and mapping entities to Ohmter devices (options flow).

Pairing asks for one thing: the code shown in the Ohmter dashboard. No Ohmter
password, no Home Assistant token, no vendor credential. Mapping shows the
household their own entities, filtered by domain and device class so the
dropdown for "charging switch" holds switches and the one for "power" holds
power sensors, and refuses to save a device with a required role missing.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util import slugify

from .const import (
    CONF_BASE_URL,
    CONF_BRIDGE_ID,
    CONF_CODE,
    CONF_NAME,
    CONF_TOKEN,
    DOMAIN,
    OPT_DEVICES,
    VERSION,
)
from .coordinator import make_transport
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


async def _pair(hass, base_url: str, code: str, name: str) -> Dict[str, Any]:
    client = OhmterClient(base_url, None, make_transport(async_get_clientsession(hass)), user_agent=f"OhmterHA/{VERSION}")
    return await client.pair(
        code, name=name, ha_version=HA_VERSION, integration_version=VERSION,
        instance_id=await async_get_instance_id(hass),
    )


class OhmterConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
            name = (user_input.get(CONF_NAME) or "Home Assistant").strip()
            try:
                body = await _pair(self.hass, base_url, user_input[CONF_CODE], name)
            except BadPairingCode as exc:
                errors["base"] = "code_expired" if exc.code == "code_expired" else "bad_code"
            except OhmterUnreachable:
                errors["base"] = "cannot_connect"
            except OhmterError:
                errors["base"] = "unknown"
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
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Dict[str, Any]) -> ConfigFlowResult:
        """Ohmter says this bridge was disconnected: ask for a fresh code."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                body = await _pair(self.hass, entry.data[CONF_BASE_URL], user_input[CONF_CODE], entry.title)
            except BadPairingCode as exc:
                errors["base"] = "code_expired" if exc.code == "code_expired" else "bad_code"
            except OhmterUnreachable:
                errors["base"] = "cannot_connect"
            except OhmterError:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: body["bridge_token"], CONF_BRIDGE_ID: body["bridge_id"]},
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_CODE): str}), errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "OhmterOptionsFlow":
        return OhmterOptionsFlow()


class OhmterOptionsFlow(OptionsFlow):
    """Add or remove the devices this Home Assistant reports to Ohmter."""

    def __init__(self) -> None:
        self._kind: Optional[str] = None

    def _devices(self) -> List[Dict[str, Any]]:
        return list(self.config_entry.options.get(OPT_DEVICES) or [])

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        options = ["add_device"]
        if self._devices():
            options.append("remove_device")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_device(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            self._kind = user_input["kind"]
            return await self.async_step_map_entities()
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

    async def async_step_map_entities(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        kind = self._kind or "ev"
        errors: Dict[str, str] = {}
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            entities = {role: user_input[role] for role in ROLES_BY_KIND[kind] if user_input.get(role)}
            missing = [r for r in REQUIRED_ROLES[kind] if r not in entities]
            if kind == "p1" and "power" not in entities and not ("import_power" in entities and "export_power" in entities):
                missing.append("power")
            if len(name) < 2:
                errors["name"] = "name_too_short"
            elif missing:
                errors["base"] = "missing_role"
            elif len(set(entities.values())) != len(entities):
                errors["base"] = "same_entity_twice"
            else:
                devices = self._devices()
                key = slugify(name)[:40] or f"{kind}-{len(devices) + 1}"
                if any(d["device_key"] == key for d in devices):
                    errors["name"] = "name_taken"
                else:
                    devices.append({"device_key": key, "kind": kind, "name": name, "entities": entities})
                    return self.async_create_entry(data={OPT_DEVICES: devices})
        fields: Dict[Any, Any] = {vol.Required("name", default=(user_input or {}).get("name", "")): str}
        for role in ROLES_BY_KIND[kind]:
            marker = vol.Required if role in REQUIRED_ROLES[kind] else vol.Optional
            fields[marker(role)] = selector.EntitySelector(_ROLE_SELECTORS[kind][role])
        return self.async_show_form(
            step_id="map_entities", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"kind": kind},
        )

    async def async_step_remove_device(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        devices = self._devices()
        if user_input is not None:
            drop = set(user_input.get("devices") or [])
            return self.async_create_entry(data={OPT_DEVICES: [d for d in devices if d["device_key"] not in drop]})
        options = [selector.SelectOptionDict(value=d["device_key"], label=f"{d['name']} ({d['kind']})") for d in devices]
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema({
                vol.Required("devices"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST)
                ),
            }),
        )
