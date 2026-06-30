"""Alarm control panel entity for the RISCO alarm exposed by the app API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import get_api
from .const import DOMAIN
from .coordinator import async_get_coordinator


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    coordinator = await async_get_coordinator(hass, "security", get_api(hass).security)
    async_add_entities([HomeAutomationAlarm(coordinator)])


class HomeAutomationAlarm(CoordinatorEntity, AlarmControlPanelEntity):
    _attr_name = "Home alarm"
    _attr_unique_id = f"{DOMAIN}_alarm"
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    @property
    def available(self) -> bool:
        return bool((self.coordinator.data or {}).get("reachable", True))

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        mode = str((self.coordinator.data or {}).get("mode") or "").lower()
        label = str((self.coordinator.data or {}).get("label") or "").lower()
        if "trigger" in mode or "alarm" in mode or "trigger" in label:
            return AlarmControlPanelState.TRIGGERED
        if mode == "disarmed" or "disarmed" in label:
            return AlarmControlPanelState.DISARMED
        if mode == "perimeter":
            return AlarmControlPanelState.ARMED_HOME
        if mode == "partial":
            return AlarmControlPanelState.ARMED_NIGHT
        if mode == "armed" or "armed" in label:
            return AlarmControlPanelState.ARMED_AWAY
        return AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "app_mode": data.get("mode"),
            "app_label": data.get("label"),
            "ac_lost": data.get("ac_lost"),
            "trouble": data.get("trouble"),
            "assumed_control_panel_state": data.get("assumed_control_panel_state"),
            "supported_actions": data.get("supported_actions"),
        }

    async def _action(self, action: str) -> None:
        await get_api(self.hass).control_security(action)
        await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._action("disarm")

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._action("arm")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        # Preserve the existing explicit RISCO perimeter command. HA's built-in
        # "arm home" intent now reaches the same app endpoint that the old
        # custom sentence/rest_command used.
        await self._action("perimeter")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        # Preserve the existing explicit RISCO partial command.
        await self._action("partial")
