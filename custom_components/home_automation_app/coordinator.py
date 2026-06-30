"""Shared polling coordinators for Home Automation App entities."""

from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import get_api, get_scan_interval
from .api import HomeAutomationApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_get_coordinator(
    hass: HomeAssistant,
    key: str,
    fetcher: Callable[[], object],
) -> DataUpdateCoordinator[Any]:
    """Return/create a shared coordinator for one backend endpoint."""
    coordinators = hass.data[DOMAIN].setdefault("coordinators", {})
    if key in coordinators:
        return coordinators[key]

    async def _async_update_data() -> Any:
        try:
            return await fetcher()
        except HomeAutomationApiError as exc:
            raise UpdateFailed(str(exc)) from exc

    coordinator: DataUpdateCoordinator[Any] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"home_automation_app_{key}",
        update_method=_async_update_data,
        update_interval=get_scan_interval(hass),
    )
    await coordinator.async_refresh()
    coordinators[key] = coordinator
    return coordinator


def item_name(item: dict[str, Any], *fallback_keys: str) -> str:
    """Best display label shared by all platforms."""
    for key in ("display_name", "name", *fallback_keys):
        value = item.get(key)
        if value:
            return str(value)
    return "Home Automation device"
