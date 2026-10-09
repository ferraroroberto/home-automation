"""Background daily blind schedule evaluator (#871) and the alarm pairing (#875).

Fires each enabled entry of ``config/blind_schedules.json`` once on its day at
its time through :func:`src.blind_automation.move_blinds`, the same parallel
group move as the Blinds card's All up / All down.

The timing follows the HVAC schedule engine (``app.webapp.automation``): a
narrow catch-up window after HH:MM (:func:`src._schedule_store.daily_due`), so
a restart hours later never replays a stale morning "up", and a once-per-day
gate per entry. An entry whose presence condition does not hold at its fire
time is skipped for the day (it does not wait for someone to arrive), and so
is one whose presence cannot be established. When every one of its blinds
failed — the LAN down, say — it is retried on the next poll inside the window.

:func:`follow_alarm` is the other way blinds move on their own: the presence
alarm automation calls it after the panel *confirmed* an automatic arm or
disarm, and it lowers every blind on a full arm or raises them on a daytime
disarm when the Blinds card's "Follow the automatic alarm" switch is on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src._schedule_store import daily_due
from src.blind_automation import (
    BlindScheduleEntry,
    alarm_blind_action,
    cover_device_ids,
    is_daytime,
    load_blind_alarm_prefs,
    load_blind_schedules,
    move_blinds,
    presence_allows,
)
from src.location_config import load_location_config
from src.presence_engine import load_people
from src.sun_position import sun_position

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


def _record(entry: BlindScheduleEntry, outcome: str, detail: str) -> None:
    _record_event(
        entry.action, entry.id, "schedule", outcome,
        {"time": entry.time, "targets": entry.targets, "detail": detail},
    )


async def _apply(entry: BlindScheduleEntry) -> bool:
    """Run one due entry; ``True`` when it is settled for today.

    Settled means it moved at least one blind, or was deliberately skipped
    (presence, no blinds left). ``False`` — every blind failed — leaves it
    due, so the next poll inside the window retries it.
    """
    allowed, why = presence_allows(entry.presence, (p.state for p in load_people().values()))
    if not allowed:
        logger.info(
            "ℹ️ Blind schedule %s (%s %s) skipped today — presence '%s' not met: %s",
            entry.id, entry.time, entry.action, entry.presence, why,
        )
        _record(entry, "skipped", why)
        return True

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
        return True

    logger.info(
        "⏰ Applying blind schedule %s (%s %s, %d blind(s), %s)",
        entry.id, entry.time, entry.action, len(targets), why,
    )
    outcomes = await move_blinds(entry.action, targets)  # type: ignore[arg-type]
    moved = [o for o in outcomes if o.ok]
    if not moved:
        _record(entry, "error", "every blind failed")
        return False
    failed = len(outcomes) - len(moved)
    _record(entry, "ok", f"{len(moved)} moved, {failed} failed")
    return True


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
            if await _apply(entry):
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
        _record_event(
            blind_action, "all", "alarm", "ok" if moved else "error",
            {"alarm": kind, "alarm_action": action, "detail": why,
             "moved": moved, "total": len(outcomes)},
        )
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
