"""
Elgato light LAN client
=======================
UI-free spike core for Elgato Key Light style devices.

Elgato lights expose a small local HTTP API on port 9123. Discovery uses the
``_elg._tcp.local.`` mDNS service when available, and ``ELGATO_LIGHT_HOSTS`` is
the static fallback for networks where Bonjour is blocked or unreliable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9123
_SERVICE_TYPE = "_elg._tcp.local."
_DISCOVERY_TIMEOUT_S = 3.0
_HTTP_TIMEOUT_S = 3.0
_MIN_BRIGHTNESS = 3
_MAX_BRIGHTNESS = 100
_MIN_TEMPERATURE = 143
_MAX_TEMPERATURE = 344


class ElgatoConfigError(RuntimeError):
    """Raised when no Elgato hosts can be discovered or configured."""


class ElgatoDiscoveryError(RuntimeError):
    """Raised when mDNS discovery cannot run."""


class ElgatoCommandError(RuntimeError):
    """Raised when a light rejects a command or returns malformed data."""


@dataclass(frozen=True)
class ElgatoEndpoint:
    """One LAN endpoint for an Elgato accessory."""

    host: str
    port: int = _DEFAULT_PORT
    name: Optional[str] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def light_id(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ElgatoLight:
    """Flattened light state safe for CLI/API/UI callers."""

    light_id: str
    host: str
    port: int
    name: str
    product_name: Optional[str]
    firmware: Optional[str]
    on: bool
    brightness: int
    temperature: int
    temperature_k: int
    supports_temperature: bool
    display_name: Optional[str] = None
    mac_address: Optional[str] = None
    reachable: bool = True
    error: Optional[str] = None


def temperature_to_kelvin(temperature: int) -> int:
    """Convert Elgato's mired temperature value to Kelvin."""
    return round(1_000_000 / int(temperature))


def kelvin_to_temperature(kelvin: int) -> int:
    """Convert Kelvin to Elgato's mired temperature value."""
    return round(1_000_000 / int(kelvin))


def _parse_host(raw: str) -> Optional[ElgatoEndpoint]:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("http://"):
        raw = raw[len("http://") :]
    if raw.startswith("https://"):
        raw = raw[len("https://") :]
    raw = raw.split("/", 1)[0].strip()
    if not raw:
        return None
    if raw.count(":") == 1:
        host, port_raw = raw.rsplit(":", 1)
        try:
            return ElgatoEndpoint(host=host.strip(), port=int(port_raw))
        except ValueError:
            return ElgatoEndpoint(host=raw, port=_DEFAULT_PORT)
    return ElgatoEndpoint(host=raw, port=_DEFAULT_PORT)


def _configured_endpoints() -> list[ElgatoEndpoint]:
    load_dotenv(override=True)
    raw = os.getenv("ELGATO_LIGHT_HOSTS", "")
    endpoints = [_parse_host(part) for part in raw.split(",")]
    return [endpoint for endpoint in endpoints if endpoint is not None]


def _discover_sync(timeout_s: float) -> list[ElgatoEndpoint]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as exc:
        raise ElgatoDiscoveryError(
            "zeroconf is not installed; set ELGATO_LIGHT_HOSTS or install requirements.txt"
        ) from exc

    class _Listener(ServiceListener):
        def __init__(self) -> None:
            self.endpoints: list[ElgatoEndpoint] = []

        def add_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            info = zc.get_service_info(service_type, name)
            if info is None:
                return
            addresses = [addr for addr in info.parsed_scoped_addresses() if ":" not in addr]
            if not addresses:
                return
            label = info.properties.get(b"mf") or info.properties.get(b"id")
            self.endpoints.append(
                ElgatoEndpoint(
                    host=addresses[0],
                    port=info.port or _DEFAULT_PORT,
                    name=label.decode("utf-8", errors="ignore") if label else name,
                )
            )

        def update_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            self.add_service(zc, service_type, name)

        def remove_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            return None

    listener = _Listener()
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, _SERVICE_TYPE, listener)
        import time

        time.sleep(timeout_s)
    finally:
        zc.close()

    unique: dict[str, ElgatoEndpoint] = {}
    for endpoint in listener.endpoints:
        unique[endpoint.light_id] = endpoint
    return list(unique.values())


async def discover_endpoints(timeout_s: float = _DISCOVERY_TIMEOUT_S) -> list[ElgatoEndpoint]:
    """Return configured endpoints plus any found by mDNS."""
    endpoints = {endpoint.light_id: endpoint for endpoint in _configured_endpoints()}
    try:
        discovered = await asyncio.to_thread(_discover_sync, timeout_s)
    except ElgatoDiscoveryError:
        if endpoints:
            return list(endpoints.values())
        raise
    for endpoint in discovered:
        endpoints[endpoint.light_id] = endpoint
    if not endpoints:
        raise ElgatoConfigError(
            "No Elgato lights found. Add ELGATO_LIGHT_HOSTS=host[:9123] to .env "
            "or make sure mDNS/Bonjour can see _elg._tcp.local."
        )
    return list(endpoints.values())


async def _get_json(session: aiohttp.ClientSession, endpoint: ElgatoEndpoint, path: str) -> dict[str, Any]:
    url = f"{endpoint.base_url}{path}"
    try:
        async with session.get(url) as response:
            if response.status >= 400:
                raise ElgatoCommandError(f"{endpoint.light_id} returned HTTP {response.status}")
            body = await response.json()
    except asyncio.TimeoutError as exc:
        raise ElgatoCommandError(f"{endpoint.light_id} timed out") from exc
    except aiohttp.ClientError as exc:
        raise ElgatoCommandError(f"{endpoint.light_id} unreachable: {exc}") from exc
    if not isinstance(body, dict):
        raise ElgatoCommandError(f"{endpoint.light_id} returned a malformed JSON payload")
    return body


async def _put_json(
    session: aiohttp.ClientSession,
    endpoint: ElgatoEndpoint,
    path: str,
    payload: dict[str, Any],
) -> None:
    url = f"{endpoint.base_url}{path}"
    try:
        async with session.put(url, json=payload) as response:
            if response.status >= 400:
                detail = await response.text()
                raise ElgatoCommandError(
                    f"{endpoint.light_id} rejected command with HTTP {response.status}: {detail}"
                )
    except asyncio.TimeoutError as exc:
        raise ElgatoCommandError(f"{endpoint.light_id} command timed out") from exc
    except aiohttp.ClientError as exc:
        raise ElgatoCommandError(f"{endpoint.light_id} command failed: {exc}") from exc


def _first_light(payload: dict[str, Any], endpoint: ElgatoEndpoint) -> dict[str, Any]:
    lights = payload.get("lights")
    if not isinstance(lights, list) or not lights or not isinstance(lights[0], dict):
        raise ElgatoCommandError(f"{endpoint.light_id} returned no light state")
    return lights[0]


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


async def read_light(endpoint: ElgatoEndpoint) -> ElgatoLight:
    """Read one Elgato light endpoint."""
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        info: dict[str, Any] = {}
        try:
            info = await _get_json(session, endpoint, "/elgato/accessory-info")
        except ElgatoCommandError as exc:
            logger.info("ℹ️ Elgato accessory info unavailable for %s: %s", endpoint.light_id, exc)
        payload = await _get_json(session, endpoint, "/elgato/lights")
    light = _first_light(payload, endpoint)
    brightness = int(light.get("brightness", 0))
    temperature = int(light.get("temperature", 0))
    supports_temperature = temperature > 0
    name = str(
        info.get("displayName")
        or endpoint.name
        or info.get("productName")
        or f"Elgato {endpoint.host}"
    )
    mac_address = _first_text(
        info,
        ("macAddress", "macaddress", "mac_address", "wifiMacAddress"),
    )
    return ElgatoLight(
        light_id=endpoint.light_id,
        host=endpoint.host,
        port=endpoint.port,
        name=name,
        product_name=info.get("productName"),
        firmware=info.get("firmwareBuildNumber") or info.get("firmwareVersion"),
        on=bool(light.get("on")),
        brightness=brightness,
        temperature=temperature,
        temperature_k=temperature_to_kelvin(temperature) if supports_temperature else 0,
        supports_temperature=supports_temperature,
        mac_address=mac_address,
    )


async def fetch_lights() -> list[ElgatoLight]:
    """Discover/configure Elgato endpoints and read each one independently."""
    endpoints = await discover_endpoints()
    lights: list[ElgatoLight] = []
    for endpoint in endpoints:
        try:
            lights.append(await read_light(endpoint))
        except ElgatoCommandError as exc:
            lights.append(
                ElgatoLight(
                    light_id=endpoint.light_id,
                    host=endpoint.host,
                    port=endpoint.port,
                    name=endpoint.name or f"Elgato {endpoint.host}",
                    product_name=None,
                    firmware=None,
                    on=False,
                    brightness=0,
                    temperature=0,
                    temperature_k=0,
                    supports_temperature=False,
                    reachable=False,
                    error=str(exc),
                )
            )
    return lights


def _clamp_int(value: int, min_value: int, max_value: int, label: str) -> int:
    value = int(value)
    if value < min_value or value > max_value:
        raise ElgatoCommandError(f"{label} must be between {min_value} and {max_value}")
    return value


async def set_light_state(
    light_id: str,
    *,
    on: Optional[bool] = None,
    brightness: Optional[int] = None,
    temperature: Optional[int] = None,
    temperature_k: Optional[int] = None,
) -> ElgatoLight:
    """Set one light, then read back the accepted live state."""
    endpoints = await discover_endpoints()
    endpoint = next((item for item in endpoints if item.light_id == light_id), None)
    if endpoint is None:
        raise ElgatoCommandError(f"Elgato light {light_id} is not configured or discoverable")

    current = await read_light(endpoint)
    next_on = current.on if on is None else bool(on)
    next_brightness = current.brightness if brightness is None else _clamp_int(
        brightness, _MIN_BRIGHTNESS, _MAX_BRIGHTNESS, "brightness"
    )
    if temperature_k is not None:
        temperature = kelvin_to_temperature(temperature_k)
    next_temperature = None
    if temperature is not None:
        if not current.supports_temperature:
            raise ElgatoCommandError(f"{light_id} does not report color-temperature support")
        next_temperature = _clamp_int(temperature, _MIN_TEMPERATURE, _MAX_TEMPERATURE, "temperature")

    payload = {
        "numberOfLights": 1,
        "lights": [
            {
                "on": 1 if next_on else 0,
                "brightness": next_brightness,
            }
        ],
    }
    if next_temperature is not None:
        payload["lights"][0]["temperature"] = next_temperature
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _put_json(session, endpoint, "/elgato/lights", payload)
    return await read_light(endpoint)
