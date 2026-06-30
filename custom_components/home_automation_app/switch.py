"""Switch entities for Tuya devices exposed by the app API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    coordinator = await async_get_coordinator(hass, "tuya", get_api(hass).tuya_devices)
    async_add_entities(
        HomeAutomationSwitch(coordinator, device["device_id"])
        for device in coordinator.data
        if device.get("has_switch")
    )


class HomeAutomationSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: Any, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_switch_{device_id}"

    @property
    def _device(self) -> dict[str, Any]:
        for device in self.coordinator.data or []:
            if str(device.get("device_id")) == self._device_id:
                return device
        return {"device_id": self._device_id}

    @property
    def name(self) -> str:
        return item_name(self._device, "device_id")

    @property
    def available(self) -> bool:
        return bool(self._device.get("reachable")) and not bool(self._device.get("hidden"))

    @property
    def is_on(self) -> bool | None:
        value = self._device.get("switch_on")
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await get_api(self.hass).set_tuya_switch(self._device_id, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await get_api(self.hass).set_tuya_switch(self._device_id, False)
        await self.coordinator.async_request_refresh()
