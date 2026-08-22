"""Background weekly RISCO alarm schedule evaluator."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from app.webapp.alarm_notify import (
    OUTCOME_ALREADY,
    OUTCOME_ERROR,
    OUTCOME_OK,
    SOURCE_SCHEDULE,
    alarm_action_already_satisfied,
    automatic_alarm_action_lock,
    confirm_alarm_action,
    record_alarm_action,
)
from src.presence_engine import note_manual_alarm_action
from src.risco_client import RiscoCommandError, RiscoConfigError, fetch_security_state
from src.security_schedules import SecurityScheduleEntry, load_security_schedules, schedule_due

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityScheduleConfig:
    """Alarm schedule engine knobs loaded from ``.env``."""

    enabled: bool = True
    poll_interval_s: int = 60

    @property
    def fire_grace_s(self) -> int:
        """Only bounds the backward look-back for a very-late-previous-night
        fire time just after local midnight — see ``schedule_due()``. A
        schedule's forward retry window (today's own fire time onward) is not
        gated by this value; it stays due for the rest of the day (#527)."""
        return max(120, self.poll_interval_s * 2)


@dataclass
class _EngineState:
    last_fire_day: Dict[str, str]


# Per-schedule once-per-day fire gate: schedule id -> "YYYY-MM-DD". Persisted
# to disk so a tray/webapp restart after a schedule's fire time does not
# refire it later the same day (#540) — mirrors alarm_notify.py's dedupe cache.
_LAST_FIRE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "security_schedule_last_fire.json"


def _load_last_fire_day() -> Dict[str, str]:
    try:
        raw = json.loads(_LAST_FIRE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_last_fire_day(state: Dict[str, str]) -> None:
    try:
        _LAST_FIRE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_FIRE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LAST_FIRE_PATH)
    except OSError as exc:
        logger.warning("⚠️ Could not persist alarm schedule fire state: %s", exc)


def load_security_schedule_config() -> SecurityScheduleConfig:
    """Read optional alarm schedule engine settings from ``.env``."""

    load_dotenv(override=True)
    return SecurityScheduleConfig(
        enabled=_env_bool("SECURITY_SCHEDULES_ENABLED", True),
        poll_interval_s=max(10, _env_int("SECURITY_SCHEDULES_POLL_INTERVAL_S", 60)),
    )


async def _apply_schedule(entry: SecurityScheduleEntry) -> None:
    logger.info("⏰ Applying alarm schedule %s (%s %s)", entry.id, entry.time, entry.action)
    detail = entry.time
    try:
        async with automatic_alarm_action_lock():
            state = await fetch_security_state()
            if alarm_action_already_satisfied(entry.action, state):
                # The panel already reports the desired end state (#676) - e.g.
                # arming when already armed, or disarming when already
                # disarmed. Skip the command entirely rather than reissuing it
                # and having the read-back confirmation misread "already
                # there" as a mismatch.
                logger.info(
                    "ℹ️ Alarm schedule %s skipped - panel already %s", entry.id, state.mode
                )
                await record_alarm_action(
                    source=SOURCE_SCHEDULE,
                    action=entry.action,
                    outcome=OUTCOME_ALREADY,
                    detail=detail,
                    reason=f"schedule {entry.id}",
                )
                return
            # Publish the deliberate command before the panel can enter its new
            # mode. Presence compares arrivals to this timestamp, so an older,
            # previously unconsumed arrival cannot undo an in-flight schedule.
            # Keeping this marker after a failed confirm is intentional: a
            # delayed panel transition still belongs to this arm request, while
            # any genuinely later arrival has a newer transition timestamp.
            note_manual_alarm_action(entry.action)
            await confirm_alarm_action(entry.action)
    except (RiscoCommandError, RiscoConfigError) as exc:
        # Every attempt (initial + all read-only retries) failed to confirm the
        # expected state, or credentials/config are missing entirely: log +
        # alert once per day, and re-raise so tick() leaves last_fire_day unset
        # and retries again on its own next poll.
        await record_alarm_action(
            source=SOURCE_SCHEDULE,
            action=entry.action,
            outcome=OUTCOME_ERROR,
            error=str(exc),
            detail=detail,
            reason=f"schedule {entry.id}",
            dedupe_key=f"schedule:{entry.id}",
        )
        raise
    await record_alarm_action(
        source=SOURCE_SCHEDULE,
        action=entry.action,
        outcome=OUTCOME_OK,
        detail=detail,
        reason=f"schedule {entry.id}",
    )


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
            _save_last_fire_day(state.last_fire_day)
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            logger.warning("⚠️ Alarm schedule apply failed for %s: %s", entry.id, exc)


async def _run(config: SecurityScheduleConfig) -> None:
    state = _EngineState(last_fire_day=_load_last_fire_day())
    await run_loop(
        lambda: tick(config, state),
        config.poll_interval_s,
        logger=logger,
        name="Alarm schedules",
        start_msg="🛡️ Alarm schedules started (poll %ds)" % config.poll_interval_s,
        tick_fail_msg="⚠️ Alarm schedule tick failed: %s",
    )


def start_security_schedules() -> Optional[asyncio.Task]:
    """Start the alarm schedule task if enabled."""

    config = load_security_schedule_config()
    if not config.enabled:
        logger.info("ℹ️ Alarm schedules disabled (SECURITY_SCHEDULES_ENABLED)")
        return None
    return asyncio.create_task(_run(config), name="security-schedules")
