"""Diagnostic sensors for the bridge itself: what Ohmter last saw and did."""
from __future__ import annotations

import datetime
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import OhmterConfigEntry
from .const import CONF_BASE_URL, CONF_BRIDGE_ID, DOMAIN
from .coordinator import OhmterCoordinator


def bridge_device_info(entry: OhmterConfigEntry) -> dict:
    return {
        "identifiers": {(DOMAIN, entry.data[CONF_BRIDGE_ID])},
        "name": entry.title,
        "manufacturer": "Ohmter",
        "model": "Home Assistant bridge",
        "configuration_url": entry.data[CONF_BASE_URL] + "/app",
    }


class _Base(CoordinatorEntity[OhmterCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OhmterCoordinator, entry: OhmterConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_BRIDGE_ID]}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = bridge_device_info(entry)

    @property
    def available(self) -> bool:
        return True  # the sensors describe the outage; they must not vanish in one


class LastSyncSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> Optional[datetime.datetime]:
        raw = self.coordinator.last_ok_sync
        if not raw:
            return None
        naive = datetime.datetime.fromisoformat(raw)
        return naive.replace(tzinfo=dt_util.get_default_time_zone()) if naive.tzinfo is None else naive


class BridgeStatusSensor(_Base):
    @property
    def native_value(self) -> str:
        return self.coordinator.bridge_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "devices": [d["device_key"] for d in self.coordinator.devices],
            "last_command": self.coordinator.last_command_note,
        }


class LastCommandSensor(_Base):
    @property
    def native_value(self) -> Optional[str]:
        return (self.coordinator.last_command_note or None)


async def async_setup_entry(hass: HomeAssistant, entry: OhmterConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        LastSyncSensor(coordinator, entry, "last_sync"),
        BridgeStatusSensor(coordinator, entry, "bridge_status"),
        LastCommandSensor(coordinator, entry, "last_command"),
    ])
