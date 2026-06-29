"""Background UPS mains-power monitor — edge-triggered power-loss alerts.

Nothing reads the UPS server-side otherwise (the Plugs/Home tiles poll it only
while a browser is open), so reliable power-lost / power-restored Telegram alerts
need their own background task. This polls :func:`src.ups_client.fetch_ups_state`
on an interval, tracks the last ``mains_online`` value in process memory, and
fires :func:`app.webapp.power_notify.record_power_event` on each mains↔battery
transition (baseline on first observation — no alert for the state at startup).

``fetch_ups_state`` is blocking (subprocess / Windows WMI), so it runs in a
thread via ``asyncio.to_thread`` to keep the event loop free. Gated by
``POWER_MONITOR_ENABLED`` (default on), mirroring the other lifespan tasks.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from app.webapp.power_notify import record_power_event
from src.ups_client import UpsState, fetch_ups_state

logger = logging.getLogger(__name__)


@dataclass
class _MonitorState:
    last_mains_online: Optional[bool] = None


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


def _runtime_detail(ups: UpsState) -> Optional[str]:
    secs = ups.runtime_seconds
    if not secs or secs <= 0:
        return None
    hours, rem = divmod(int(secs), 3600)
    minutes = rem // 60
    return (f"{hours}h {minutes}min" if hours else f"{minutes}min") + " runtime"


async def tick(state: _MonitorState) -> None:
    """Read the UPS once and alert on a mains↔battery transition."""

    ups = await asyncio.to_thread(fetch_ups_state)
    if ups is None or not ups.available or ups.mains_online is None:
        return
    online = ups.mains_online
    if state.last_mains_online is None:
        state.last_mains_online = online  # baseline — never alert on startup state
        return
    if state.last_mains_online == online:
        return

    lost = state.last_mains_online is True and online is False
    state.last_mains_online = online
    record_power_event(lost=lost, detail=_runtime_detail(ups) if lost else None)


async def _run(interval_s: int) -> None:
    logger.info("🔌 Power monitor started (poll %ds)", interval_s)
    state = _MonitorState()
    try:
        while True:
            try:
                await tick(state)
            except Exception as exc:  # noqa: BLE001 - a read failure never kills the loop
                logger.warning("⚠️ Power monitor tick failed: %s", exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("🛑 Power monitor stopped")
        raise


def start_power_monitor() -> Optional[asyncio.Task]:
    """Start the UPS power monitor task if enabled."""

    load_dotenv(override=True)
    if not _env_bool("POWER_MONITOR_ENABLED", True):
        logger.info("ℹ️ Power monitor disabled (POWER_MONITOR_ENABLED)")
        return None
    interval_s = max(10, _env_int("POWER_MONITOR_POLL_INTERVAL_S", 60))
    return asyncio.create_task(_run(interval_s), name="power-monitor")
