"""Background UPS mains-power monitor — edge-triggered power-loss alerts.

Nothing reads the UPS server-side otherwise (the Plugs/Home tiles poll it only
while a browser is open), so reliable power-lost / power-restored Telegram alerts
need their own background task. This polls :func:`src.ups_client.fetch_ups_state`
on an interval, tracks the last ``mains_online`` value in process memory, and
fires :func:`app.webapp.power_notify.record_power_event` on each mains↔battery
transition (baseline on first observation — no alert for the state at startup).

It also drives the low-battery safety shutdown: whenever the UPS is on battery
and its reported ``runtime_seconds`` drops to :data:`LOW_BATTERY_RUNTIME_THRESHOLD_S`
(15 min) or below, :func:`app.webapp.power_notify.record_low_battery_shutdown`
fires once per outage (edge-triggered on a process-memory flag, not on the
mains↔battery transition itself — the battery can keep draining for several
polls after the outage starts before it crosses the threshold). Unlike the
mains-transition baseline skip, this check is **not** suppressed on the first
observation: if the monitor starts while the UPS is already critically low
(e.g. the webapp restarted mid-outage), it still triggers — this is a safety
measure, not a Telegram-spam-avoidance one. If mains power returns before the
scheduled OS shutdown completes, :func:`app.webapp.power_notify.record_low_battery_shutdown_cancelled`
aborts it and resets the flag so a later outage can trigger again.

``fetch_ups_state`` is blocking (subprocess / Windows WMI), so it runs in a
thread via ``asyncio.to_thread`` to keep the event loop free. Gated by
``POWER_MONITOR_ENABLED`` (default on), mirroring the other lifespan tasks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp.power_notify import (
    record_low_battery_shutdown,
    record_low_battery_shutdown_cancelled,
    record_power_event,
)
from src.ups_client import UpsState, fetch_ups_state

logger = logging.getLogger(__name__)

# Hardcoded, not user-configurable — only whether the feature is on/off is
# (via PowerNotifyPrefs.auto_shutdown_low_battery).
LOW_BATTERY_RUNTIME_THRESHOLD_S = 15 * 60


@dataclass
class _MonitorState:
    last_mains_online: Optional[bool] = None
    low_battery_shutdown_triggered: bool = False


def _runtime_detail(ups: UpsState) -> Optional[str]:
    secs = ups.runtime_seconds
    if not secs or secs <= 0:
        return None
    hours, rem = divmod(int(secs), 3600)
    minutes = rem // 60
    return (f"{hours}h {minutes}min" if hours else f"{minutes}min") + " runtime"


async def tick(state: _MonitorState) -> None:
    """Read the UPS once, alert on a mains↔battery transition, and enforce the
    low-battery safety shutdown while on battery."""

    ups = await asyncio.to_thread(fetch_ups_state)
    if ups is None or not ups.available or ups.mains_online is None:
        return
    online = ups.mains_online
    baseline = state.last_mains_online is None
    changed = not baseline and state.last_mains_online != online

    if changed:
        lost = state.last_mains_online is True and online is False
        await record_power_event(lost=lost, detail=_runtime_detail(ups) if lost else None)

    state.last_mains_online = online

    if online:
        if state.low_battery_shutdown_triggered:
            state.low_battery_shutdown_triggered = False
            record_low_battery_shutdown_cancelled()
        return

    if (
        not state.low_battery_shutdown_triggered
        and ups.runtime_seconds is not None
        and ups.runtime_seconds <= LOW_BATTERY_RUNTIME_THRESHOLD_S
    ):
        state.low_battery_shutdown_triggered = True
        await record_low_battery_shutdown(detail=_runtime_detail(ups))


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
