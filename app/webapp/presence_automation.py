"""Background presence -> alarm automation consumer."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from app.webapp.alarm_notify import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    SOURCE_PRESENCE,
    automatic_alarm_action_lock,
    check_security_transitions,
    confirm_alarm_action,
    record_alarm_action,
)
from app.webapp.alarm_scene_automation import consider_security_read
from app.webapp.security_override_automation import (
    consider_security_read as consider_security_override,
)
from src.presence_engine import (
    PresenceDecision,
    append_trigger_log,
    evaluate_alarm_decision,
    load_automation_config,
    load_kids_home_override,
    load_people,
    mark_decision_applied,
    set_kids_home_override,
)
from src.presence_hidden import load_hidden_presence_ids
from src.push_notifications import send_push
from src.risco_client import fetch_security_state

logger = logging.getLogger(__name__)


def _evaluate_current_decision(security_mode: str) -> Optional[PresenceDecision]:
    """Load current presence inputs and evaluate one alarm decision."""

    config = load_automation_config()
    if not config.enabled:
        return None
    hidden = load_hidden_presence_ids()
    people = [p for pid, p in load_people().items() if pid not in hidden]
    if not people:
        return None
    return evaluate_alarm_decision(
        people,
        security_mode=security_mode,
        config=config,
        at=datetime.now(timezone.utc),
        override_perimeter=load_kids_home_override(),
    )


async def tick() -> None:
    """Alert on panel events, then evaluate one presence transition."""

    # Panel-event alerts (intrusion / AC-power lost-restored) ride on this loop's
    # one security read and fire regardless of the presence auto-arm toggle —
    # those alerts must not depend on auto-arm being enabled. This is the only
    # interval reader of RISCO state, so adding a second poller would just risk
    # the cloud's third-party rate limit; intrusion/AC alerts therefore require
    # this task to be running (PRESENCE_AUTOMATION_ENGINE_ENABLED, default on).
    security = await fetch_security_state()
    ongoing, memory = security.ongoing_alarm, security.memory_alarm
    # None,None means the RISCO WebUI scrape that backs these two flags came
    # back unreadable this poll — not "no alarm" (issue #307: a transient
    # scrape hiccup was mistaken for the alarm clearing, so the *next*
    # successful poll re-observing a still-latched, days-old memory_alarm
    # manufactured a false→true "new" intrusion and paged for nothing).
    intrusion = None if ongoing is None and memory is None else bool(ongoing or memory)
    await check_security_transitions(
        intrusion=intrusion,
        ac_lost=bool(security.ac_lost),
        intrusion_detail=f"ongoing_alarm={ongoing} memory_alarm={memory}",
    )
    # Same single read drives the alarm-triggered camera scene capture + AI
    # verdict (issue #162): cheap edge detection here, heavy capture/vision work
    # dispatched as a detached task so it never blocks this poll.
    consider_security_read(security)
    # ...and the configurable per-detector auto-bypass-after-N-repeats override
    # (issue #341): runs every tick (not just while an alarm is active) so it
    # also catches the arm event that restores a previously bypassed zone.
    consider_security_override(security)

    decision = _evaluate_current_decision(security.mode)
    if decision is None:
        return

    outcome = "started"
    failure: Optional[Exception] = None
    async with automatic_alarm_action_lock():
        # The first read may have raced a schedule while waiting for this lock.
        # Re-read both the panel and persisted presence/command timestamps before
        # acting so a decision is never applied from a stale snapshot.
        security = await fetch_security_state()
        refreshed_decision = _evaluate_current_decision(security.mode)
        if refreshed_decision is None:
            logger.info(
                "ℹ️ Presence automation skipped stale %s decision after coordinated re-check",
                decision.kind,
            )
            return
        decision = refreshed_decision
        try:
            updated = await confirm_alarm_action(decision.action)
            outcome = updated.mode
            mark_decision_applied(decision, outcome)
            # Someone arrived and the system disarmed: clear the transient kids-home
            # override so the next away-cycle defaults back to a full arm.
            if decision.kind == "disarm":
                set_kids_home_override(False)
        except Exception as exc:  # noqa: BLE001
            outcome = f"error: {exc}"
            failure = exc

    try:
        if failure is None:
            logger.info("✅ Presence automation %s -> %s", decision.reason, decision.action)
            send_push("Presence automation", f"{decision.reason}: {decision.action}")
            await record_alarm_action(
                source=SOURCE_PRESENCE,
                action=decision.action,
                outcome=OUTCOME_OK,
                detail=decision.reason,
            )
        else:
            logger.warning("⚠️ Presence automation action failed: %s", failure)
            # Failure leaves the decision un-applied, so the loop retries every tick;
            # de-dupe the alert to once per day per presence transition kind.
            await record_alarm_action(
                source=SOURCE_PRESENCE,
                action=decision.action,
                outcome=OUTCOME_ERROR,
                error=str(failure),
                detail=decision.reason,
                dedupe_key=f"presence:{decision.kind}",
            )
    finally:
        append_trigger_log(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "consumer": "alarm",
                "event": decision.kind,
                "action": decision.action,
                "reason": decision.reason,
                "transition_at": decision.transition_at.isoformat(),
                "outcome": outcome,
            }
        )


async def _run(interval_s: int) -> None:
    await run_loop(
        tick,
        interval_s,
        logger=logger,
        name="Presence alarm automation",
        start_msg="🛡️ Presence alarm automation started (poll %ds)" % interval_s,
        tick_fail_msg="⚠️ Presence automation tick failed: %s",
    )


def start_presence_automation() -> Optional[asyncio.Task]:
    """Start the presence automation task; config defaults make it a no-op."""

    load_dotenv(override=True)
    if not _env_bool("PRESENCE_AUTOMATION_ENGINE_ENABLED", True):
        logger.info("ℹ️ Presence automation engine disabled")
        return None
    interval_s = max(5, _env_int("PRESENCE_AUTOMATION_POLL_INTERVAL_S", 10))
    return asyncio.create_task(_run(interval_s), name="presence-automation")
