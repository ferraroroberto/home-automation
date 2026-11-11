"""Background weekly RISCO alarm schedule evaluator."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

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
from src._schedule_store import DailyFireLatch, FireLatchPolicy
from src.local_time import HOME_TZ, to_home_wall
from src.presence.presence_engine import note_manual_alarm_action
from src.security.risco_client import (
    RiscoCommandError,
    RiscoConfigError,
    fetch_security_state,
)
from src.security.security_schedules import (
    SecurityScheduleEntry,
    load_security_schedules,
    schedule_due,
)

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
        gated by this value; it stays due for the rest of the day."""
        return max(120, self.poll_interval_s * 2)


# The window stays open for the rest of the day so a failed arm or disarm
# keeps retrying; only a confirmed apply spends the day.
FIRE_LATCH_POLICY = FireLatchPolicy(
    name="Alarm schedule",
    file_name="security_schedule_last_fire.json",
    window="rest_of_day",
)


@dataclass
class _EngineState:
    latch: DailyFireLatch = field(default_factory=lambda: DailyFireLatch(FIRE_LATCH_POLICY))


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
                # The panel already reports the desired end state - e.g.
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
        # Every attempt (initial + all re-read/resend retries) failed to confirm the
        # expected state, or credentials/config are missing entirely: log +
        # alert once per day, and re-raise so tick() leaves the day unspent
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
    """Apply every due enabled schedule at most once per Europe/Madrid date.

    Schedule times are Madrid wall times, so the clock is read in that zone
    rather than the host's, keeping fires on time across a DST change.
    """

    schedules = load_security_schedules()
    if not any(entry.enabled for entry in schedules):
        return

    instant = to_home_wall(now or datetime.now(HOME_TZ))
    today = instant.strftime("%Y-%m-%d")
    for entry in schedules:
        if not entry.enabled:
            continue
        due = schedule_due(entry, instant, config.fire_grace_s)
        state.latch.note_window(entry.id, today, due)
        if not due or state.latch.fired(entry.id, today):
            continue
        try:
            await state.latch.fire_once(entry.id, today, lambda e=entry: _apply_schedule(e))
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            logger.warning("⚠️ Alarm schedule apply failed for %s: %s", entry.id, exc)


async def _run(config: SecurityScheduleConfig) -> None:
    state = _EngineState()
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
