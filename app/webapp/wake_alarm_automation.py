"""Background wake-alarm + timer evaluator (issue #304).

Polls the persisted wake-alarm list and the in-memory timer store, marks due
entries "ringing", and fires a best-effort Telegram notify. One-shot alarms
(a ``date`` rather than ``days``) disable themselves after firing; weekly
ones rearm for their next matching day automatically (nothing to do — the
day/time check just won't match again until it recurs).

"Ringing" is deliberately not part of the persisted alarm entry (it's
transient UI/notify state, not configuration) — it lives in this module's
in-memory set, cleared by :func:`dismiss_alarm`. Timer ringing state lives on
the timer entry itself in :mod:`src.wake_timers` (cleared by cancelling it —
a rung timer has nothing left to configure).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Optional, Set

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src.activity_log import append_activity
from src.notify import NotifierError
from src.notify_config import build_alarm_notifier
from src.wake_alarms import (
    WakeAlarmEntry,
    load_wake_alarms,
    save_wake_alarms,
    wake_alarm_due,
)
from src.wake_timers import WakeTimerEntry, mark_expired

logger = logging.getLogger(__name__)

_ringing_alarm_ids: Set[str] = set()


@dataclass(frozen=True)
class WakeAlarmConfig:
    """Wake-alarm engine knobs loaded from ``.env``."""

    enabled: bool = True
    poll_interval_s: int = 15

    @property
    def fire_grace_s(self) -> int:
        return max(60, self.poll_interval_s * 2)


@dataclass
class _EngineState:
    last_fire_day: Dict[str, str]


def load_wake_alarm_config() -> WakeAlarmConfig:
    """Read optional wake-alarm engine settings from ``.env``."""

    load_dotenv(override=True)
    return WakeAlarmConfig(
        enabled=_env_bool("WAKE_ALARMS_ENABLED", True),
        poll_interval_s=max(5, _env_int("WAKE_ALARMS_POLL_INTERVAL_S", 15)),
    )


def ringing_alarm_ids() -> Set[str]:
    """Ids of alarms currently ringing, awaiting dismissal."""

    return set(_ringing_alarm_ids)


def dismiss_alarm(alarm_id: str) -> bool:
    """Clear an alarm's ringing state. ``True`` if it was ringing."""

    if alarm_id in _ringing_alarm_ids:
        _ringing_alarm_ids.discard(alarm_id)
        return True
    return False


async def _notify(message: str) -> None:
    notifier = build_alarm_notifier()
    if notifier is None:
        return
    try:
        # notifier.send_text is blocking network I/O called from an async tick
        # sharing uvicorn's single event loop — thread it off so a slow/failing
        # send can't stall the webapp.
        await asyncio.to_thread(notifier.send_text, message)
    except NotifierError as exc:  # delivery must never break the loop
        logger.warning("⚠️ Wake-alarm notify failed: %s", exc)


async def _fire_alarm(entry: WakeAlarmEntry) -> None:
    logger.info("⏰ Wake alarm ringing: %s (%s)", entry.label or entry.id, entry.time)
    _ringing_alarm_ids.add(entry.id)
    await _notify(f"⏰ Wake alarm ringing — {entry.label or entry.time}")
    append_activity("wake_alarms", {"kind": "alarm", "id": entry.id, "label": entry.label, "time": entry.time})


async def _fire_timer(entry: WakeTimerEntry) -> None:
    logger.info("⏱️ Timer ringing: %s (%ss)", entry.label or entry.id, entry.seconds)
    await _notify(f"⏱️ Timer ringing — {entry.label or (str(entry.seconds) + 's')}")
    append_activity("wake_alarms", {"kind": "timer", "id": entry.id, "label": entry.label, "seconds": entry.seconds})


async def test_fire_alarm(alarm_id: str) -> bool:
    """Fire one alarm's ring immediately, regardless of its schedule. ``True`` if found."""

    for entry in load_wake_alarms():
        if entry.id == alarm_id:
            await _fire_alarm(entry)
            return True
    return False


async def tick(config: WakeAlarmConfig, state: _EngineState, now: Optional[datetime] = None) -> None:
    """Fire every due alarm at most once per local date; expire due timers."""

    instant = now or datetime.now()
    today = instant.strftime("%Y-%m-%d")

    alarms = load_wake_alarms()
    updated = False
    next_alarms = []
    for entry in alarms:
        due = (
            entry.enabled
            and wake_alarm_due(entry, instant, config.fire_grace_s)
            and state.last_fire_day.get(entry.id) != today
        )
        if due:
            state.last_fire_day[entry.id] = today
            try:
                await _fire_alarm(entry)
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                logger.warning("⚠️ Wake alarm fire failed for %s: %s", entry.id, exc)
            if entry.date:  # one-shot — disable after firing
                entry = replace(entry, enabled=False)
                updated = True
        next_alarms.append(entry)
    if updated:
        save_wake_alarms(next_alarms)

    for timer in mark_expired(instant.timestamp()):
        try:
            await _fire_timer(timer)
        except Exception as exc:  # noqa: BLE001 — never kill the loop
            logger.warning("⚠️ Timer fire failed for %s: %s", timer.id, exc)


async def _run(config: WakeAlarmConfig) -> None:
    state = _EngineState(last_fire_day={})
    await run_loop(
        lambda: tick(config, state),
        config.poll_interval_s,
        logger=logger,
        name="Wake alarms",
        start_msg="⏰ Wake alarms started (poll %ds)" % config.poll_interval_s,
        tick_fail_msg="⚠️ Wake-alarm tick failed: %s",
    )


def start_wake_alarms() -> Optional[asyncio.Task]:
    """Start the wake-alarm/timer task if enabled."""

    config = load_wake_alarm_config()
    if not config.enabled:
        logger.info("ℹ️ Wake alarms disabled (WAKE_ALARMS_ENABLED)")
        return None
    return asyncio.create_task(_run(config), name="wake-alarms")
