"""Binary sensors for RISCO zones exposed by the app API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_api
from .const import DOMAIN
from .coordinator import async_get_coordinator, item_name


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    coordinator = await async_get_coordinator(hass, "security", get_api(hass).security)
    async_add_entities(
        HomeAutomationZoneBinarySensor(coordinator, zone["id"])
        for zone in (coordinator.data or {}).get("zones") or []
    )


class HomeAutomationZoneBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator: Any, zone_id: str | int) -> None:
        super().__init__(coordinator)
        self._zone_id = str(zone_id)
        self._attr_unique_id = f"{DOMAIN}_zone_{self._zone_id}"

    @property
    def _zone(self) -> dict[str, Any]:
        for zone in (self.coordinator.data or {}).get("zones") or []:
            if str(zone.get("id")) == self._zone_id:
                return zone
        return {"id": self._zone_id}

    @property
    def name(self) -> str:
        return item_name(self._zone, "id")

    @property
    def available(self) -> bool:
        return not bool(self._zone.get("hidden"))

    @property
    def is_on(self) -> bool:
        return bool(self._zone.get("triggered"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self._zone
        return {
            "bypassed": zone.get("bypassed"),
            "trouble": zone.get("trouble"),
            "trouble_ignored": zone.get("trouble_ignored"),
            "original_name": zone.get("name"),
            "zone_type": zone.get("type"),
        }
