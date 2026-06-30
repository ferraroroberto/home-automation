"""Home Assistant custom integration for this app's API-first backend."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from aiohttp import ClientSession
from homeassistant.const import CONF_PLATFORM
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .api import HomeAutomationApi
from .const import (
    CONF_BASE_URL,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("climate", "switch", "alarm_control_panel", "binary_sensor", "sensor")

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_BASE_URL): cv.string,
                vol.Optional(CONF_TOKEN, default=""): cv.string,
                vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_SECONDS
                ): vol.Coerce(int),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _shared_config(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data[DOMAIN]["config"]


def get_api(hass: HomeAssistant) -> HomeAutomationApi:
    """Return the shared app API client."""
    return hass.data[DOMAIN]["api"]


def get_scan_interval(hass: HomeAssistant) -> timedelta:
    """Return the configured polling interval."""
    return timedelta(seconds=max(5, int(_shared_config(hass)[CONF_SCAN_INTERVAL])))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML.

    YAML is deliberate for now: the repo already owns/deploys HA YAML for voice,
    and this keeps the first native-entity bridge reviewable as code. A config
    flow can wrap the same API client later without moving entity logic.
    """
    domain_config = dict(config.get(DOMAIN) or {})
    session = ClientSession()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["session"] = session
    hass.data[DOMAIN]["config"] = domain_config
    hass.data[DOMAIN]["api"] = HomeAutomationApi(
        session,
        domain_config[CONF_BASE_URL],
        domain_config.get(CONF_TOKEN),
        bool(domain_config.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
    )

    async def _close_session(event: object) -> None:
        await session.close()

    hass.bus.async_listen_once("homeassistant_stop", _close_session)

    for platform in PLATFORMS:
        hass.async_create_task(
            async_load_platform(hass, platform, DOMAIN, {CONF_PLATFORM: platform}, config)
        )
    _LOGGER.info("Home Automation App integration loaded for %s", domain_config[CONF_BASE_URL])
    return True
