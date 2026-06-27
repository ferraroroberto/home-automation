"""Background weekly RISCO alarm schedule evaluator."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from dotenv import load_dotenv

from src.presence_engine import note_manual_alarm_action
from src.risco_client import control_system
from src.security_schedules import SecurityScheduleEntry, load_security_schedules, schedule_due

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityScheduleConfig:
    """Alarm schedule engine knobs loaded from ``.env``."""

    enabled: bool = True
    poll_interval_s: int = 60

    @property
    def fire_grace_s(self) -> int:
        return max(120, self.poll_interval_s * 2)


@dataclass
class _EngineState:
    last_fire_day: Dict[str, str]


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


def load_security_schedule_config() -> SecurityScheduleConfig:
    """Read optional alarm schedule engine settings from ``.env``."""

    load_dotenv(override=True)
    return SecurityScheduleConfig(
        enabled=_env_bool("SECURITY_SCHEDULES_ENABLED", True),
        poll_interval_s=max(10, _env_int("SECURITY_SCHEDULES_POLL_INTERVAL_S", 60)),
    )


async def _apply_schedule(entry: SecurityScheduleEntry) -> None:
    logger.info("⏰ Applying alarm schedule %s (%s %s)", entry.id, entry.time, entry.action)
    await control_system(entry.action)
    # Record the scheduled action the same way a manual one is recorded, so the
    # presence automation won't immediately undo it (e.g. disarm a perimeter the
    # 11pm schedule just armed because people are home). A real away→home arrival
    # afterwards still disarms, since that advances the person's transition time.
    note_manual_alarm_action(entry.action)


async def tick(config: SecurityScheduleConfig, state: _EngineState, now: Optional[datetime] = None) -> None:
    """Apply every due enabled schedule at most once per local date."""

    schedules = load_security_schedules()
    if not any(entry.enabled for entry in schedules):
        return

    instant = now or datetime.now()
    today = instant.strftime("%Y-%m-%d")
    for entry in schedules:
        if not entry.enabled or not schedule_due(entry, instant, config.fire_grace_s):
            continue
        if state.last_fire_day.get(entry.id) == today:
            continue
        try:
            await _apply_schedule(entry)
            state.last_fire_day[entry.id] = today
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            logger.warning("⚠️ Alarm schedule apply failed for %s: %s", entry.id, exc)


async def _run(config: SecurityScheduleConfig) -> None:
    logger.info("🛡️ Alarm schedules started (poll %ds)", config.poll_interval_s)
    state = _EngineState(last_fire_day={})
    try:
        while True:
            try:
                await tick(config, state)
            except Exception as exc:  # noqa: BLE001 - a read failure never kills the loop
                logger.warning("⚠️ Alarm schedule tick failed: %s", exc)
            await asyncio.sleep(config.poll_interval_s)
    except asyncio.CancelledError:
        logger.info("🛑 Alarm schedules stopped")
        raise


def start_security_schedules() -> Optional[asyncio.Task]:
    """Start the alarm schedule task if enabled."""

    config = load_security_schedule_config()
    if not config.enabled:
        logger.info("ℹ️ Alarm schedules disabled (SECURITY_SCHEDULES_ENABLED)")
        return None
    return asyncio.create_task(_run(config), name="security-schedules")
