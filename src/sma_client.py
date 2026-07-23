"""
SMA energy client
=================
Non-UI core: read the home's live energy flow, ahead of the solar
load-balancing automation (shift HVAC load to match PV).

Preferred source:

* **Sunny Portal cloud energy balance** — when ``SMA_CLOUD_PLANT_ID`` is set,
  read the same live widget values shown in the SMA Energy app.

Local fallback sources:

* **Sunny Home Manager 2.0 / Energy Meter** — read over **Speedwire**
  (UDP multicast, *no credentials*).  Gives the grid connection point:
  import power, export power, and the cumulative import/export counters.
* **PV inverter** (Tripower X / ennexOS) — read over the inverter's local
  ``ennexOS`` web API, logging in with the SMA account credentials.  Gives
  the AC power the panels are producing right now.

SMA inverters power **down at night**, so an unreachable
inverter is reported as ``inverter_reachable=False`` with ``pv_power_w=None``
(PV unknown) — deliberately distinct from a real read error, which is logged.

Wraps ``pysma-plus`` (``pysmaplus``).  Shared by the CLI
(``src/list_energy.py``) and the webapp (``GET /api/energy``) so the
device-access logic lives in exactly one place.

Config (from ``.env``):

* ``SMA_INVERTER_HOST`` — inverter LAN IP/host (optional; blank → meter only)
* ``SMA_INVERTER_ACCESS_METHOD`` — ``ennexos`` (default) or ``speedwireinvV2``
* ``SMA_CLOUD_PLANT_ID`` — Sunny Portal plant/component ID (optional)
* ``SMA_CLOUD_MAX_STALENESS_S`` — discard a cloud point older than this many
  seconds and fall through to the live local sources (default 900)
* ``SMA_USER`` / ``SMA_PASSWORD`` — SMA account, for cloud and ennexOS login
* ``SMA_INVERTER_PASSWORD`` — local inverter password for Speedwire devices
* ``SMA_INVERTER_GROUP`` — Speedwire group, ``user`` (default) or ``installer``

The energy meter needs no host and no credentials — it is discovered on the
multicast group automatically.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Optional

import aiohttp
import pysmaplus
from dotenv import load_dotenv

from src.device_address import DeviceAddressError, resolve_device_host

logger = logging.getLogger("sma")

_SMA_TOKEN_URL = "https://login.sma.energy/auth/realms/SMA/protocol/openid-connect/token"
_SMA_UI_API_BASE = "https://uiapi.sunnyportal.com/api/v1"

# Energy-meter (Speedwire) sensor names exposed by pysma-plus.
_EM_IMPORT_W = "metering_power_absorbed"   # power drawn FROM the grid (W)
_EM_EXPORT_W = "metering_power_supplied"   # power fed INTO the grid (W)
_EM_IMPORT_KWH = "metering_total_absorbed"  # cumulative grid import (kWh)
_EM_EXPORT_KWH = "metering_total_yield"     # cumulative grid export (kWh)

# Inverter sensor names, best first. AC output is the power that actually
# offsets the house load; DC generator power is a fallback when AC is absent.
_INV_PV_POWER_KEYS = ("GridMs.TotW", "grid_power", "PvGen.PvW", "pv_power")

# How old the cloud energy-balance point may be before it is treated as stale
# and the read falls through to the live local sources (issue #94). Generous
# enough for normal Sunny Portal lag and the widget's ~5-min resolution, tight
# enough to catch a multi-hour freeze. Override with SMA_CLOUD_MAX_STALENESS_S.
_DEFAULT_CLOUD_MAX_STALENESS_S = 900


@dataclass
class EnergyState:
    """Flattened snapshot of the home's instantaneous energy flow.

    All powers are in watts, signed from the house's point of view where it
    matters: ``pv_surplus_w`` is positive when exporting (PV covers the load
    with power to spare — the signal to shift more HVAC load on) and negative
    when importing from the grid.  ``None`` means "not measured right now"
    (e.g. PV while the inverter is asleep) — never silently coerced to 0.
    """

    grid_import_w: Optional[float] = None
    grid_export_w: Optional[float] = None
    pv_power_w: Optional[float] = None
    house_consumption_w: Optional[float] = None
    pv_surplus_w: Optional[float] = None
    grid_import_kwh: Optional[float] = None
    grid_export_kwh: Optional[float] = None
    meter_reachable: bool = False
    inverter_reachable: bool = False
    meter_serial: Optional[str] = None


@dataclass(frozen=True)
class EnergyConfig:
    """Runtime SMA config loaded from ``.env``."""

    host: Optional[str]
    user: Optional[str]
    password: Optional[str]
    cloud_password: Optional[str]
    access_method: str
    group: str
    cloud_plant_id: Optional[str]
    cloud_max_staleness_s: int = _DEFAULT_CLOUD_MAX_STALENESS_S


@dataclass
class _CloudToken:
    """Cached SMA cloud token."""

    access_token: str
    refresh_token: Optional[str]
    expires_at: float


_cloud_token: Optional[_CloudToken] = None


def _load_config() -> EnergyConfig:
    """Read SMA inverter settings from ``.env``.

    A missing inverter host simply means the meter is read on its own (still a
    useful snapshot: grid import/export). ``SMA_INVERTER_PASSWORD`` lets
    Speedwire installations use a short local inverter password without
    overloading the cloud-account ``SMA_PASSWORD``.
    """
    load_dotenv(override=True)
    host = (os.getenv("SMA_INVERTER_HOST") or "").strip() or None
    user = (os.getenv("SMA_USER") or "").strip() or None
    access_method = (
        os.getenv("SMA_INVERTER_ACCESS_METHOD") or "ennexos"
    ).strip() or "ennexos"
    inverter_password = (os.getenv("SMA_INVERTER_PASSWORD") or "").strip()
    cloud_password = (os.getenv("SMA_PASSWORD") or "").strip() or None
    password = (
        inverter_password
        if access_method in {"speedwireinv", "speedwireinvV2"}
        else (inverter_password or cloud_password)
    ) or None
    group = (os.getenv("SMA_INVERTER_GROUP") or "user").strip() or "user"
    if group not in {"user", "installer"}:
        logger.warning("⚠️ Invalid SMA_INVERTER_GROUP=%s; using user", group)
        group = "user"
    cloud_plant_id = (os.getenv("SMA_CLOUD_PLANT_ID") or "").strip() or None
    raw_staleness = (os.getenv("SMA_CLOUD_MAX_STALENESS_S") or "").strip()
    try:
        cloud_max_staleness_s = (
            int(raw_staleness) if raw_staleness else _DEFAULT_CLOUD_MAX_STALENESS_S
        )
    except ValueError:
        logger.warning(
            "⚠️ Invalid SMA_CLOUD_MAX_STALENESS_S=%s; using %s",
            raw_staleness, _DEFAULT_CLOUD_MAX_STALENESS_S,
        )
        cloud_max_staleness_s = _DEFAULT_CLOUD_MAX_STALENESS_S
    return EnergyConfig(
        host, user, password, cloud_password, access_method, group,
        cloud_plant_id, cloud_max_staleness_s,
    )


def _cloud_is_stale(payload_time: object, max_staleness_s: int) -> bool:
    """True if the cloud widget's as-of timestamp is older than the freshness window.

    The Sunny Portal ``energybalance`` widget echoes the timestamp of its latest
    data point in ``time`` (naive local ISO, e.g. ``2026-06-22T13:30:00``). When
    the Sunny Home Manager stops uploading, the widget keeps returning that same
    point unchanged, so a frozen value looks "live" (issue #94). A missing or
    unparseable timestamp is treated as fresh — staleness cannot be proven, so an
    otherwise-good read is not discarded.
    """
    if not payload_time:
        return False
    try:
        as_of = datetime.fromisoformat(str(payload_time))
        # The widget timestamp is naive local; drop any tz so the subtraction
        # against a naive ``now()`` never raises on a tz-aware variant.
        age = (datetime.now() - as_of.replace(tzinfo=None)).total_seconds()
    except (ValueError, TypeError):
        logger.debug("SMA cloud time %r unparseable; treating as fresh", payload_time)
        return False
    return age > max_staleness_s


async def _read_meter(state: EnergyState) -> None:
    """Read the Speedwire energy meter into ``state`` (best-effort)."""
    device = pysmaplus.getDevice(None, "", accessmethod="speedwireem")
    if device is None:  # pragma: no cover - defensive
        logger.warning("⚠️ Could not create the Speedwire energy-meter device")
        return
    try:
        await device.new_session()
        info = await device.device_info()
        state.meter_serial = str(info.get("serial")) if info else None
        sensors = await device.get_sensors()
        await device.read(sensors)
        values = {s.name: s.value for s in sensors if s.value is not None}

        state.grid_import_w = values.get(_EM_IMPORT_W)
        state.grid_export_w = values.get(_EM_EXPORT_W)
        state.grid_import_kwh = values.get(_EM_IMPORT_KWH)
        state.grid_export_kwh = values.get(_EM_EXPORT_KWH)
        state.meter_reachable = state.grid_import_w is not None
        if state.meter_reachable:
            logger.info(
                "✅ Meter %s: import %s W, export %s W",
                state.meter_serial, state.grid_import_w, state.grid_export_w,
            )
    except Exception as exc:  # noqa: BLE001 - any Speedwire/parse error
        logger.warning("⚠️ Energy-meter read failed: %s", exc)
    finally:
        try:
            await device.close_session()
        except Exception:  # noqa: BLE001
            pass


async def _read_inverter(
    state: EnergyState, config: EnergyConfig
) -> None:
    """Read PV production from the configured inverter into ``state``.

    A timeout or connection refusal is the normal night-time case (the
    inverter sleeps) — logged at info, leaving ``pv_power_w=None`` and
    ``inverter_reachable=False``.
    """
    if config.access_method == "ennexos":
        await _read_ennexos_inverter(state, config)
        return
    if config.access_method in {"speedwireinv", "speedwireinvV2"}:
        await _read_speedwire_inverter(state, config)
        return
    logger.warning("⚠️ Unsupported SMA_INVERTER_ACCESS_METHOD=%s", config.access_method)


async def _get_cloud_token(config: EnergyConfig) -> Optional[str]:
    """Return an SMA cloud access token using the portal OAuth password grant."""
    global _cloud_token

    if not config.user or not config.cloud_password:
        logger.info("ℹ️ SMA cloud credentials not set; skipping cloud energy read")
        return None
    now = time.monotonic()
    if _cloud_token and _cloud_token.expires_at - now > 30:
        return _cloud_token.access_token

    data = {
        "client_id": "SPpbeOS",
        "scope": "openid profile",
    }
    if _cloud_token and _cloud_token.refresh_token:
        data.update({
            "grant_type": "refresh_token",
            "refresh_token": _cloud_token.refresh_token,
        })
    else:
        data.update({
            "grant_type": "password",
            "username": config.user,
            "password": config.cloud_password,
        })

    async with aiohttp.ClientSession() as session:
        async with session.post(_SMA_TOKEN_URL, data=data, timeout=20) as response:
            if response.status >= 400:
                # If a cached refresh token expired, retry once with password grant.
                if data["grant_type"] == "refresh_token":
                    _cloud_token = None
                    return await _get_cloud_token(config)
                text = await response.text()
                logger.warning("⚠️ SMA cloud login failed: HTTP %s %s", response.status, text[:200])
                return None
            payload = await response.json()

    access_token = payload.get("access_token")
    if not access_token:
        logger.warning("⚠️ SMA cloud login returned no access token")
        return None
    _cloud_token = _CloudToken(
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        expires_at=now + int(payload.get("expires_in") or 300),
    )
    return access_token


async def _read_cloud_energy(state: EnergyState, config: EnergyConfig) -> bool:
    """Read the Sunny Portal energy-balance widget into ``state``."""
    if not config.cloud_plant_id:
        return False
    token = await _get_cloud_token(config)
    if not token:
        return False

    url = f"{_SMA_UI_API_BASE}/widgets/energybalance"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    params = {"componentId": config.cloud_plant_id}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params, timeout=20) as response:
            if response.status >= 400:
                text = await response.text()
                logger.warning("⚠️ SMA cloud energy read failed: HTTP %s %s", response.status, text[:200])
                return False
            payload = await response.json()

    # A frozen cloud point (Sunny Home Manager stopped uploading) keeps coming
    # back unchanged and would be recorded as a fresh live sample, flat-lining
    # the chart (issue #94). Honour the widget's own as-of timestamp: when it is
    # stale, treat the cloud read as unavailable so we fall through to the live
    # local Speedwire sources.
    if _cloud_is_stale(payload.get("time"), config.cloud_max_staleness_s):
        logger.warning(
            "⚠️ SMA cloud data stale (as-of %s, older than %ds) — "
            "falling back to local reads",
            payload.get("time"), config.cloud_max_staleness_s,
        )
        return False

    state.grid_import_w = _as_float(payload.get("externalConsumption"))
    state.grid_export_w = _as_float(payload.get("feedIn"))
    state.pv_power_w = _as_float(payload.get("pvGeneration"))
    state.house_consumption_w = _as_float(payload.get("totalConsumption"))
    state.pv_surplus_w = (
        round((state.grid_export_w or 0) - (state.grid_import_w or 0), 1)
        if state.grid_import_w is not None and state.grid_export_w is not None
        else None
    )
    state.meter_reachable = state.grid_import_w is not None or state.grid_export_w is not None
    state.inverter_reachable = state.pv_power_w is not None
    logger.info(
        "✅ SMA cloud plant %s (as-of %s): PV %s W, import %s W, consumption %s W",
        config.cloud_plant_id,
        payload.get("time"),
        state.pv_power_w,
        state.grid_import_w,
        state.house_consumption_w,
    )
    return True


def _as_float(value: object) -> Optional[float]:
    """Convert API numbers to float while preserving missing values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _read_ennexos_inverter(state: EnergyState, config: EnergyConfig) -> None:
    """Read PV production from an ennexOS inverter into ``state``."""
    if config.host is None:  # pragma: no cover - guarded by caller
        return
    url = config.host if "://" in config.host else f"https://{config.host}"
    # The inverter serves a self-signed cert; skip verification on the LAN.
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        device = pysmaplus.getDevice(
            session, url, password=config.password, groupuser=config.user or "user",
            accessmethod="ennexos",
        )
        if device is None:  # pragma: no cover - defensive
            logger.warning("⚠️ Could not create the ennexOS inverter device")
            return
        try:
            if not await device.new_session():
                logger.info(
                    "ℹ️ Inverter at %s did not authenticate "
                    "(asleep, or wrong SMA credentials)", url,
                )
                return
            sensors = await device.get_sensors()
            await device.read(sensors)
            values = {s.name: s.value for s in sensors if s.value is not None}
            for key in _INV_PV_POWER_KEYS:
                value = values.get(key)
                if value is not None:
                    state.pv_power_w = float(value)
                    break
            state.inverter_reachable = True
            logger.info("✅ Inverter %s: PV %s W", url, state.pv_power_w)
        except Exception as exc:  # noqa: BLE001 - timeout = asleep, etc.
            logger.info("ℹ️ Inverter at %s not reachable: %s", url, exc)
        finally:
            try:
                await device.close_session()
            except Exception:  # noqa: BLE001
                pass


async def _read_speedwire_inverter(state: EnergyState, config: EnergyConfig) -> None:
    """Read PV production from a Speedwire inverter into ``state``."""
    if config.host is None:  # pragma: no cover - guarded by caller
        return
    if not config.password:
        logger.info("ℹ️ Speedwire inverter password not set; skipping PV inverter")
        return
    device = pysmaplus.getDevice(
        None,
        config.host,
        password=config.password,
        groupuser=config.group,
        accessmethod=config.access_method,
    )
    if device is None:  # pragma: no cover - defensive
        logger.warning("⚠️ Could not create the Speedwire inverter device")
        return
    try:
        if not await device.new_session():
            logger.info("ℹ️ Speedwire inverter at %s did not authenticate", config.host)
            return
        sensors = await device.get_sensors()
        await device.read(sensors)
        values = {s.name: s.value for s in sensors if s.value is not None}
        for key in _INV_PV_POWER_KEYS:
            value = values.get(key)
            if value is not None:
                state.pv_power_w = float(value)
                break
        state.inverter_reachable = True
        logger.info("✅ Speedwire inverter %s: PV %s W", config.host, state.pv_power_w)
    except Exception as exc:  # noqa: BLE001 - timeout/auth/asleep/etc.
        logger.info("ℹ️ Speedwire inverter at %s not reachable: %s", config.host, exc)
    finally:
        try:
            await device.close_session()
        except Exception:  # noqa: BLE001
            pass


def _derive(state: EnergyState) -> None:
    """Fill the computed fields (consumption, surplus) from the raw reads."""
    imp = state.grid_import_w
    exp = state.grid_export_w
    if imp is None or exp is None:
        return
    # Signed net export: + when PV covers the load with power to spare,
    # - when drawing from the grid. One of import/export is always ~0.
    state.pv_surplus_w = round(exp - imp, 1)
    if state.pv_power_w is not None:
        # consumption = production + what we pull − what we push back
        state.house_consumption_w = round(state.pv_power_w + imp - exp, 1)
    else:
        # PV unknown (inverter asleep): at night export≈0 so the grid draw
        # is the whole house load — a correct estimate while PV is zero.
        state.house_consumption_w = imp


async def fetch_energy_state() -> EnergyState:
    """Read every reachable SMA energy source and return a flattened snapshot.

    Never raises for a missing/asleep device — partial data (meter only) is a
    normal, useful result.  The reachability flags say what was actually read.
    """
    config = _load_config()
    state = EnergyState()

    if await _read_cloud_energy(state, config):
        return state

    logger.info("ℹ️ Reading SMA energy meter (Speedwire)")
    await _read_meter(state)

    if config.host:
        # SMA_INVERTER_HOST may be a MAC rather than an IP (issue #504); a
        # literal IP/hostname passes straight through with no lookup. Pinning by
        # MAC is what stops a DHCP reshuffle silently pointing this read at
        # whatever device inherited the inverter's old address.
        try:
            host = await resolve_device_host(config.host)
        except DeviceAddressError as exc:
            logger.warning("⚠️ PV inverter address could not be resolved: %s", exc)
            host = None
        if host:
            logger.info(
                "ℹ️ Reading PV inverter at %s (%s)",
                host,
                config.access_method,
            )
            await _read_inverter(state, replace(config, host=host))
        else:
            # Same shape as an unreachable inverter: a snapshot without PV is
            # still useful, so this must not fail the whole energy read.
            logger.info("ℹ️ PV inverter address unavailable — skipping inverter read")
    else:
        logger.info("ℹ️ SMA_INVERTER_HOST not set — skipping PV inverter read")

    _derive(state)
    return state
