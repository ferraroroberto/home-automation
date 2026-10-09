"""Background daily blind schedule evaluator (issue #871).

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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src._schedule_store import daily_due
from src.blind_automation import (
    BlindScheduleEntry,
    cover_device_ids,
    load_blind_schedules,
    move_blinds,
    presence_allows,
)
from src.presence_engine import load_people

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


def _record(entry: BlindScheduleEntry, outcome: str, detail: str) -> None:
    """Best-effort telemetry event, like the plug toggle's (#289)."""
    try:
        from src import telemetry

        telemetry.record_event(
            "blind",
            "blind_" + entry.action,
            entity_id=entry.id,
            source="schedule",
            outcome=outcome,
            payload={"time": entry.time, "targets": entry.targets, "detail": detail},
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        logger.debug("telemetry blind-schedule event skipped", exc_info=True)


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
