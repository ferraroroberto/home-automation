"""Background telemetry-reading sampler — owned by the webapp (uvicorn) lifecycle.

A sibling of :mod:`app.webapp.sampler` (which owns the SMA energy series). This
one snapshots the *other* device domains — HVAC temps, plug watts, UPS load,
Elgato lights — into the unified :mod:`src.telemetry` ``readings`` table on a
gentle cadence, so the Activity log's readings view has history to draw.

Design mirrors the energy sampler: one asyncio task started in the FastAPI
lifespan, blocking fetches/writes off the event loop via ``asyncio.to_thread``,
and a per-domain try/except so one flaky device never kills the loop. Each
domain has its own ``.env`` gate, and the master ``TELEMETRY_SAMPLER_ENABLED``
switch turns the whole thing off (e2e/dev runs).

Cadence defaults to 5 minutes (``TELEMETRY_SAMPLE_INTERVAL_S``): temperature and
load trends don't need 60 s resolution, and the gentler cadence keeps cloud/LAN
polling light. Energy stays in :mod:`src.energy_history`; presence is event-
driven (#289) and so its reading gate defaults off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from src import telemetry
from src import telemetry_adapters as adapters

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelemetrySamplerConfig:
    """Sampler cadence + per-domain gates, from ``.env`` (all optional)."""

    enabled: bool = True
    interval_s: int = 300
    compact_interval_s: int = 3600
    hvac: bool = True
    plugs: bool = True
    ups: bool = True
    lights: bool = True
    presence: bool = False  # presence is event-driven (#289); readings off by default


def load_sampler_config() -> TelemetrySamplerConfig:
    load_dotenv(override=True)
    return TelemetrySamplerConfig(
        enabled=_env_bool("TELEMETRY_SAMPLER_ENABLED", True),
        interval_s=max(30, _env_int("TELEMETRY_SAMPLE_INTERVAL_S", 300)),
        compact_interval_s=max(60, _env_int("TELEMETRY_COMPACT_INTERVAL_S", 3600)),
        hvac=_env_bool("TELEMETRY_SAMPLE_HVAC", True),
        plugs=_env_bool("TELEMETRY_SAMPLE_PLUGS", True),
        ups=_env_bool("TELEMETRY_SAMPLE_UPS", True),
        lights=_env_bool("TELEMETRY_SAMPLE_LIGHTS", True),
        presence=_env_bool("TELEMETRY_SAMPLE_PRESENCE", False),
    )


# ----------------------------------------------------- per-domain collectors
# Each returns a list[Reading] or raises; the loop isolates failures per domain.
async def _collect_hvac() -> list:
    from src.melcloud_client import fetch_devices

    return adapters.hvac_readings(await fetch_devices())


async def _collect_plugs() -> list:
    from src.tuya_client import list_devices, read_device_state

    def _read_all() -> list:
        states = []
        seen = set()
        for info in list_devices():
            dev_id = info.device_id
            if not dev_id or dev_id in seen or not info.has_local_key or not info.has_valid_ip:
                continue
            seen.add(dev_id)
            try:
                states.append(read_device_state(dev_id))
            except Exception:  # noqa: BLE001 — one offline plug must not drop the rest
                states.append({"device_id": dev_id, "reachable": False})
        return states

    return adapters.plug_readings(await asyncio.to_thread(_read_all))


async def _collect_ups() -> list:
    from src.ups_client import fetch_ups_state

    return adapters.ups_readings(await asyncio.to_thread(fetch_ups_state))


async def _collect_lights() -> list:
    from src.elgato_client import fetch_lights

    return adapters.light_readings(await fetch_lights())


_COLLECTORS = {
    "hvac": _collect_hvac,
    "plugs": _collect_plugs,
    "ups": _collect_ups,
    "lights": _collect_lights,
}


def _enabled_domains(config: TelemetrySamplerConfig) -> list:
    return [name for name in _COLLECTORS if getattr(config, name)]


async def _sample_once(config: TelemetrySamplerConfig) -> int:
    """Sample every enabled domain once; return the rows written. Never raises."""
    written = 0
    for name in _enabled_domains(config):
        try:
            rows = await _COLLECTORS[name]()
            written += await asyncio.to_thread(telemetry.record_readings, rows)
        except Exception as exc:  # noqa: BLE001 — isolate per domain
            logger.warning("⚠️ Telemetry %s sample failed: %s", name, exc)
    return written


async def _run(config: TelemetrySamplerConfig) -> None:
    await asyncio.to_thread(telemetry.init_db)
    logger.info(
        "📊 Telemetry sampler started (every %ds; domains: %s)",
        config.interval_s,
        ", ".join(_enabled_domains(config)) or "none",
    )
    last_compact = 0.0
    try:
        while True:
            await _sample_once(config)
            now = time.monotonic()
            if now - last_compact >= config.compact_interval_s:
                try:
                    await asyncio.to_thread(telemetry.compact_and_prune)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("⚠️ Telemetry compaction failed: %s", exc)
                last_compact = now
            await asyncio.sleep(config.interval_s)
    except asyncio.CancelledError:
        logger.info("🛑 Telemetry sampler stopped")
        raise


def start_telemetry_sampler() -> "asyncio.Task | None":
    """Start the reading sampler if enabled; return it (or ``None`` when off)."""
    config = load_sampler_config()
    if not config.enabled:
        logger.info("ℹ️ Telemetry sampler disabled (TELEMETRY_SAMPLER_ENABLED)")
        return None
    if not _enabled_domains(config):
        logger.info("ℹ️ Telemetry sampler: no domains enabled — not starting")
        return None
    return asyncio.create_task(_run(config), name="telemetry-sampler")
