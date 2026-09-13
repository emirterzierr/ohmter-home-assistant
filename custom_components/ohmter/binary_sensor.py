"""One binary sensor: is this bridge talking to Ohmter right now."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OhmterConfigEntry
from .const import CONF_BRIDGE_ID
from .coordinator import OhmterCoordinator
from .sensor import bridge_device_info


class OhmterConnectedSensor(CoordinatorEntity[OhmterCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connected"

    def __init__(self, coordinator: OhmterCoordinator, entry: OhmterConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_BRIDGE_ID]}_connected"
        self._attr_device_info = bridge_device_info(entry)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.bridge_status == "ok"


async def async_setup_entry(hass: HomeAssistant, entry: OhmterConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([OhmterConnectedSensor(entry.runtime_data, entry)])
