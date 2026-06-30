"""Energy sensors exposed by the app API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_api
from .const import DOMAIN
from .coordinator import async_get_coordinator

_POWER_SENSORS = {
    "grid_import_w": "Grid import",
    "grid_export_w": "Grid export",
    "pv_power_w": "PV power",
    "house_consumption_w": "House consumption",
    "pv_surplus_w": "PV surplus",
}
_ENERGY_SENSORS = {
    "grid_import_kwh": "Grid import energy",
    "grid_export_kwh": "Grid export energy",
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    coordinator = await async_get_coordinator(hass, "energy", get_api(hass).energy)
    entities = [HomeAutomationEnergySensor(coordinator, key, name, False) for key, name in _POWER_SENSORS.items()]
    entities.extend(
        HomeAutomationEnergySensor(coordinator, key, name, True) for key, name in _ENERGY_SENSORS.items()
    )
    async_add_entities(entities)


class HomeAutomationEnergySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: Any, key: str, name: str, cumulative: bool) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_energy_{key}"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR if cumulative else UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.ENERGY if cumulative else SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING if cumulative else SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get(self._key)
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "meter_reachable": data.get("meter_reachable"),
            "inverter_reachable": data.get("inverter_reachable"),
            "meter_serial": data.get("meter_serial"),
        }
