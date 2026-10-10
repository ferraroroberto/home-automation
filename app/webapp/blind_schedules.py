"""Background daily blind schedule evaluator (#871) and the alarm pairing (#875).

Fires each enabled entry of ``config/blind_schedules.json`` once on its day at
its time through :func:`src.blind_automation.move_blinds`, the same parallel
group move as the Blinds card's All up / All down.

The timing follows the HVAC schedule engine (``app.webapp.automation``): a
narrow catch-up window after HH:MM (:func:`src._schedule_store.daily_due`), so
a restart hours later never replays a stale morning "up", and a once-per-day
gate per entry. An entry whose presence condition does not hold at its fire
time is skipped for the day (it does not wait for someone to arrive), and so
is one whose presence cannot be established. Only a move that could not even
be attempted (an exception, say a missing ``devices.json``) is retried on the
next poll inside the window.

:func:`follow_alarm` is the other way blinds move on their own: the presence
alarm automation calls it after the panel *confirmed* an automatic arm or
disarm, and it lowers every blind on a full arm or raises them on a daytime
disarm when the Blinds card's "Follow the automatic alarm" switch is on.

Both automatic paths retry a blind that failed — offline when the move fired —
in the background through :func:`src.blind_automation.retry_failed_blinds`
(#897), so a retry never delays the schedule tick or the alarm path. The event
record is written once the move has settled, with its attempt count; a blind
still failing after the last retry sends one Telegram message per move.
A manual tap on the Blinds card gets the same retry (#899) through
:func:`settle_manual_move`: the card hears the first attempt at once, and the
retries carry on in the background.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src._schedule_store import daily_due
from src.blind_automation import (
    BlindOutcome,
    BlindRetryReport,
    BlindScheduleEntry,
    alarm_blind_action,
    cover_device_ids,
    is_daytime,
    load_blind_alarm_prefs,
    load_blind_schedules,
    move_blinds,
    presence_allows,
    retry_delays,
    retry_failed_blinds,
)
from src.location_config import load_location_config
from src.notify import NotifierError
from src.notify_config import build_alarm_notifier
from src.presence_engine import load_people
from src.sun_position import sun_position
from src.tuya_client import list_devices
from src.tuya_display_names import load_tuya_display_names

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlindScheduleConfig:
    """Blind schedule engine knobs loaded from ``.env``."""

    enabled: bool = True
    poll_interval_s: int = 60

    @property
    def fire_grace_s(self) -> int:
        """The HVAC engine's catch-up window: two polls or two minutes."""
        return max(120, self.poll_interval_s * 2)


@dataclass
class _EngineState:
    # entry id -> local date it last fired (or was deliberately skipped).
    last_fire_day: Dict[str, str] = field(default_factory=dict)


def load_blind_schedule_config() -> BlindScheduleConfig:
    """Read optional blind schedule engine settings from ``.env``."""
    load_dotenv(override=True)
    return BlindScheduleConfig(
        enabled=_env_bool("BLIND_SCHEDULES_ENABLED", True),
        poll_interval_s=max(10, _env_int("BLIND_SCHEDULES_POLL_INTERVAL_S", 60)),
    )


def _record_event(
    action: str, entity_id: str, source: str, outcome: str, payload: Dict[str, Any]
) -> None:
    """Best-effort telemetry event, like the plug toggle's (#289)."""
    try:
        from src import telemetry

        telemetry.record_event(
            "blind",
            "blind_" + action,
            entity_id=entity_id,
            source=source,
            outcome=outcome,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        logger.debug("telemetry blind event skipped", exc_info=True)


def _record(
    entry: BlindScheduleEntry, outcome: str, detail: str, extra: Optional[Dict[str, Any]] = None
) -> None:
    _record_event(
        entry.action, entry.id, "schedule", outcome,
        {"time": entry.time, "targets": entry.targets, "detail": detail, **(extra or {})},
    )


# ----------------------------------------------------- retries (#897)
# Background retries of automatic moves, held so none is garbage-collected
# mid-backoff.
_RETRY_TASKS: Set[asyncio.Task] = set()


async def _retry_sleep(delay: float) -> None:
    """The backoff's clock; tests replace it with a fake one."""
    await asyncio.sleep(delay)


def _report_outcome(report: BlindRetryReport) -> str:
    return "error" if report.failed else "ok"


def _report_fields(report: BlindRetryReport) -> Dict[str, Any]:
    """The settled move for the event record: final outcome and attempt count."""
    detail = f"{len(report.moved)} moved, {len(report.failed)} failed"
    if report.superseded:
        detail += f", {len(report.superseded)} superseded"
    detail += f" after {report.max_attempts} attempt(s)"
    return {
        "detail": detail,
        "moved": len(report.moved),
        "failed": report.failed,
        "superseded": report.superseded,
        "attempts": report.max_attempts,
    }


def _blind_names(device_ids: Iterable[str]) -> List[str]:
    """Each blind's display name: the rename override, else its device name."""
    overrides = load_tuya_display_names()
    try:
        names = {info.device_id: info.name for info in list_devices()}
    except Exception:  # noqa: BLE001 — a name lookup must never cost the message
        names = {}
    return [overrides.get(d) or names.get(d) or d for d in device_ids]


async def _notify(text: str) -> None:
    notifier = build_alarm_notifier()
    if notifier is None:
        logger.info("ℹ️ Blind failure not sent to Telegram: notifier not configured")
        return
    try:
        # send_text is blocking network I/O; keep it off the event loop.
        await asyncio.to_thread(notifier.send_text, text)
    except NotifierError as exc:  # delivery must never break the engine
        logger.warning("⚠️ Blind failure Telegram notify failed: %s", exc)


def _retried_for(action: str) -> str:
    """How long ``action``'s retries ran, in words: "~15 s", "~7.5 min"."""
    total = sum(retry_delays(action))
    return f"~{total:g} s" if total < 60 else f"~{total / 60:g} min"


async def _finish_move(
    action: str,
    outcomes: List[BlindOutcome],
    what: str,
    record: Optional[Callable[[BlindRetryReport], None]],
) -> None:
    """Retry a move's failed blinds, then record, log and notify.

    Full success (first try or after retries) is an info log; a blind still
    failing after the last retry sends exactly one Telegram message for the
    whole move, never one per blind or per attempt. Never raises.
    """
    try:
        report = await retry_failed_blinds(action, outcomes, sleep=_retry_sleep)  # type: ignore[arg-type]
        if record is not None:
            record(report)
        if not report.failed:
            logger.info(
                "✅ Blinds %s (%s): %d moved, %d superseded, in %d attempt(s)",
                action, what, len(report.moved), len(report.superseded), report.max_attempts,
            )
            return
        logger.warning(
            "⚠️ Blinds %s (%s): %s still failing after %d attempt(s)",
            action, what, ", ".join(report.failed), report.max_attempts,
        )
        await _notify(
            f"🪟 Blinds could not {action}: {', '.join(_blind_names(report.failed))} "
            f"({what}). Retried for {_retried_for(action)}."
        )
    except Exception as exc:  # noqa: BLE001 — a background retry must never die unseen
        logger.warning("⚠️ Blind %s retry (%s) failed: %s", action, what, exc)


async def _settle(
    action: str,
    outcomes: List[BlindOutcome],
    what: str,
    record: Optional[Callable[[BlindRetryReport], None]],
) -> None:
    """Finish a move: at once when no blind failed, else in the background.

    The retries run as their own task, so they never hold up the schedule
    tick, the alarm path or the HTTP request that started the move.
    """
    if all(o.ok or o.superseded for o in outcomes):
        await _finish_move(action, outcomes, what, record)
        return
    task = asyncio.create_task(_finish_move(action, outcomes, what, record), name="blind-retry")
    _RETRY_TASKS.add(task)
    task.add_done_callback(_RETRY_TASKS.discard)


async def settle_manual_move(action: str, outcomes: List[BlindOutcome]) -> None:
    """Retry the blinds a manual Blinds-card tap failed to move (#899).

    The same backoff, supersede rule and single final-failure Telegram as the
    automatic moves; only a failure is retried in the background, so the
    request answers with the first attempt at once. No event record: the
    owner is at the app.
    """
    await _settle(action, outcomes, "manual", None)


async def _apply(entry: BlindScheduleEntry) -> None:
    """Run one due entry, which settles it for today.

    It moves its blinds (failed ones are retried in the background) or is
    deliberately skipped (presence, no blinds left). Only an exception leaves
    it due, so the next poll inside the window tries again.
    """
    allowed, why = presence_allows(entry.presence, (p.state for p in load_people().values()))
    if not allowed:
        logger.info(
            "ℹ️ Blind schedule %s (%s %s) skipped today — presence '%s' not met: %s",
            entry.id, entry.time, entry.action, entry.presence, why,
        )
        _record(entry, "skipped", why)
        return

    known = cover_device_ids()
    targets = [d for d in entry.targets if d in known] if entry.targets else known
    missing = [d for d in entry.targets if d not in known]
    if missing:
        logger.warning(
            "⚠️ Blind schedule %s names blind(s) no longer in devices.json: %s",
            entry.id, ", ".join(missing),
        )
    if not targets:
        logger.warning("⚠️ Blind schedule %s has no blinds left to move — skipped", entry.id)
        _record(entry, "skipped", "no blinds")
        return

    logger.info(
        "⏰ Applying blind schedule %s (%s %s, %d blind(s), %s)",
        entry.id, entry.time, entry.action, len(targets), why,
    )
    outcomes = await move_blinds(entry.action, targets)  # type: ignore[arg-type]

    def _record_report(report: BlindRetryReport) -> None:
        fields = _report_fields(report)
        _record(entry, _report_outcome(report), fields.pop("detail"), fields)

    await _settle(entry.action, outcomes, f"schedule {entry.time}", _record_report)


async def tick(
    config: BlindScheduleConfig, state: _EngineState, now: Optional[datetime] = None
) -> None:
    """Apply every due enabled entry at most once per local date."""
    schedules = load_blind_schedules()
    if not any(entry.enabled for entry in schedules):
        return
    instant = now or datetime.now()
    today = instant.strftime("%Y-%m-%d")
    for entry in schedules:
        if not entry.enabled or state.last_fire_day.get(entry.id) == today:
            continue
        if not daily_due(entry.time, instant, config.fire_grace_s, entry.days):
            continue
        try:
            await _apply(entry)
            state.last_fire_day[entry.id] = today
        except Exception as exc:  # noqa: BLE001 — never kill the loop
            logger.warning("⚠️ Blind schedule apply failed for %s: %s", entry.id, exc)


def _sun_elevation_now() -> Optional[float]:
    """The sun's elevation at home right now, or ``None`` with no location."""
    location = load_location_config()
    if location is None:
        return None
    return sun_position(time.time(), location.lat, location.lon).elevation_deg


async def follow_alarm(kind: str, action: str, now: Optional[datetime] = None) -> Optional[str]:
    """Move the blinds after a *confirmed* automatic alarm decision (#875).

    ``kind``/``action`` are the presence decision's (``arm``/``disarm`` and
    the panel action). Returns the blind action sent, or ``None`` when the
    blinds were left alone. Never raises: the alarm action already happened,
    and nothing here may change its outcome, record or notification.
    """
    try:
        enabled = load_blind_alarm_prefs().follow_alarm
        daytime: Optional[bool] = None
        day_why = ""
        if enabled and kind == "disarm":
            daytime, day_why = is_daytime(
                now or datetime.now(), load_blind_schedules(), _sun_elevation_now()
            )
        blind_action, why = alarm_blind_action(kind, action, enabled=enabled, daytime=daytime)
        if day_why:
            why = f"{why} ({day_why})"
        if blind_action is None:
            if enabled:
                logger.info("ℹ️ Blinds left alone after alarm %s/%s: %s", kind, action, why)
            return None
        outcomes = await move_blinds(blind_action)  # type: ignore[arg-type]
        moved = sum(1 for o in outcomes if o.ok)
        logger.info(
            "🪟 Blinds %s after alarm %s/%s — %s; %d of %d moved",
            blind_action, kind, action, why, moved, len(outcomes),
        )

        def _record_report(report: BlindRetryReport) -> None:
            fields = _report_fields(report)
            _record_event(
                blind_action, "all", "alarm", _report_outcome(report),
                {"alarm": kind, "alarm_action": action, "detail": why,
                 "result": fields.pop("detail"), "total": len(outcomes), **fields},
            )

        await _settle(blind_action, outcomes, "following the alarm", _record_report)
        return blind_action
    except Exception as exc:  # noqa: BLE001 — never let blinds touch the alarm path
        logger.warning("⚠️ Blinds could not follow the alarm %s/%s: %s", kind, action, exc)
        return None


async def _run(config: BlindScheduleConfig) -> None:
    state = _EngineState()
    await run_loop(
        lambda: tick(config, state),
        config.poll_interval_s,
        logger=logger,
        name="Blind schedules",
        start_msg="🪟 Blind schedules started (poll %ds)" % config.poll_interval_s,
        tick_fail_msg="⚠️ Blind schedule tick failed: %s",
    )


def start_blind_schedules() -> Optional[asyncio.Task]:
    """Start the blind schedule task if enabled."""
    config = load_blind_schedule_config()
    if not config.enabled:
        logger.info("ℹ️ Blind schedules disabled (BLIND_SCHEDULES_ENABLED)")
        return None
    return asyncio.create_task(_run(config), name="blind-schedules")
