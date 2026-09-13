"""Ohmter for Home Assistant - the in-home bridge.

Runs inside the household's own Home Assistant and dials *out* to Ohmter:
nothing is opened on the router, Ohmter never reaches into the house, and the
only secret is a bridge token Ohmter issued when the household typed a
one-time pairing code from their dashboard. What the household's Home
Assistant can do, Ohmter can schedule; nothing more.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BASE_URL, CONF_TOKEN
from .coordinator import OhmterCoordinator, make_transport
from .ohmter_client import OhmterClient, OhmterError

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# A plain alias rather than a `type` statement: the server's test suite parses
# this file on Python 3.10, which has no `type` keyword.
OhmterConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: OhmterConfigEntry) -> bool:
    coordinator = OhmterCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: OhmterConfigEntry) -> None:
    # The mapping changed: reload so the coordinator re-sends the device list.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OhmterConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_shutdown()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The household deleted the integration: tell Ohmter, best effort.

    If this fails the server ages the bridge out as offline and the dashboard
    offers "Disconnect"; nothing is left able to command this Home Assistant
    because the token is forgotten with the entry.
    """
    client = OhmterClient(entry.data[CONF_BASE_URL], entry.data.get(CONF_TOKEN), make_transport(async_get_clientsession(hass)))
    try:
        await client.unpair()
    except OhmterError:
        pass
