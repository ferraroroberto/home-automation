"""Climate entities for MELCloud Home units exposed by the app API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACMode,
    ClimateEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_api
from .const import DOMAIN
from .coordinator import async_get_coordinator, item_name

_MODE_TO_HVAC = {
    "Off": HVACMode.OFF,
    "Heat": HVACMode.HEAT,
    "Cool": HVACMode.COOL,
    "Dry": HVACMode.DRY,
    "Fan": HVACMode.FAN_ONLY,
    "Automatic": HVACMode.HEAT_COOL,
}
_HVAC_TO_MODE = {v: k for k, v in _MODE_TO_HVAC.items() if v is not HVACMode.OFF}


async def async_setup_platform(
    hass: HomeAssistant, config: dict[str, Any], async_add_entities: AddEntitiesCallback, discovery_info: dict[str, Any] | None = None
) -> None:
    coordinator = await async_get_coordinator(hass, "units", get_api(hass).units)
    async_add_entities(HomeAutomationClimate(coordinator, unit["unit_id"]) for unit in coordinator.data)


class HomeAutomationClimate(CoordinatorEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    def __init__(self, coordinator: Any, unit_id: str) -> None:
        super().__init__(coordinator)
        self._unit_id = unit_id
        self._attr_unique_id = f"{DOMAIN}_climate_{unit_id}"

    @property
    def _unit(self) -> dict[str, Any]:
        for unit in self.coordinator.data or []:
            if str(unit.get("unit_id")) == self._unit_id:
                return unit
        return {"unit_id": self._unit_id}

    @property
    def name(self) -> str:
        return item_name(self._unit, "unit_id")

    @property
    def available(self) -> bool:
        return bool(self._unit.get("unit_id"))

    @property
    def current_temperature(self) -> float | None:
        return self._unit.get("room_temperature")

    @property
    def target_temperature(self) -> float | None:
        return self._unit.get("set_temperature")

    @property
    def target_temperature_step(self) -> float:
        return float(self._unit.get("temp_step") or 0.5)

    @property
    def min_temp(self) -> float:
        mode = self._unit.get("operation_mode") or "Cool"
        ranges = self._unit.get("temp_ranges") or {}
        values = ranges.get(mode) or ranges.get("Cool") or [16, 31]
        return float(values[0])

    @property
    def max_temp(self) -> float:
        mode = self._unit.get("operation_mode") or "Cool"
        ranges = self._unit.get("temp_ranges") or {}
        values = ranges.get(mode) or ranges.get("Cool") or [16, 31]
        return float(values[1])

    @property
    def hvac_mode(self) -> HVACMode:
        if not self._unit.get("power"):
            return HVACMode.OFF
        return _MODE_TO_HVAC.get(str(self._unit.get("operation_mode")), HVACMode.AUTO)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = [HVACMode.OFF]
        for mode in self._unit.get("operation_modes") or []:
            hvac_mode = _MODE_TO_HVAC.get(str(mode))
            if hvac_mode and hvac_mode not in modes:
                modes.append(hvac_mode)
        return modes

    @property
    def fan_mode(self) -> str | None:
        return self._unit.get("fan_speed")

    @property
    def fan_modes(self) -> list[str] | None:
        modes = [str(value) for value in self._unit.get("fan_speeds") or []]
        return modes or ["Auto"]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        payload: dict[str, Any] = {"power": hvac_mode is not HVACMode.OFF}
        if hvac_mode is not HVACMode.OFF and hvac_mode in _HVAC_TO_MODE:
            payload["operation_mode"] = _HVAC_TO_MODE[hvac_mode]
        await get_api(self.hass).control_unit(self._unit_id, payload)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_TEMPERATURE not in kwargs:
            return
        await get_api(self.hass).control_unit(
            self._unit_id, {"power": True, "set_temperature": kwargs[ATTR_TEMPERATURE]}
        )
        await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await get_api(self.hass).control_unit(self._unit_id, {"fan_speed": fan_mode})
        await self.coordinator.async_request_refresh()
