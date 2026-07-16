"""Background HVAC automation engine — owned by the webapp (uvicorn) lifecycle.

A single asyncio task started in the FastAPI lifespan (mirrors
:mod:`app.webapp.sampler`), so it lives and dies with the webapp process the
tray owns — no separate daemon. Every ``poll_interval_s`` it reads every unit's
live state once and, per unit:

* **Schedule entries** — if the unit has enabled entries whose daily ``HH:MM``
  just came due (edge-triggered, once per local day per entry, within a short
  grace window so a midday restart does not replay the morning profile), apply
  each entry once. Power-off entries send only ``power=False``; power-on entries
  can apply the full profile (mode, setpoint, fan, vanes).
* **Rule** — if the unit is **on** and its current mode is steerable, nudge the
  unit's setpoint one step toward the rule's desired *room* target, but no more
  often than ``adjust_interval_s`` (the room responds slowly; over-nudging would
  overshoot). The rule never touches power — see :mod:`src.hvac_automation`.

The pure decision lives in :mod:`src.hvac_automation`; this module only owns the
loop, the timing/edge state, and the MELCloud reads/writes. Gated by
``HVAC_AUTOMATION_ENABLED`` (``.env``) so the e2e suite and dev runs never drive
real units. Never lets a per-unit error kill the loop (sampler pattern).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src.hvac_automation import (
    load_rules,
    load_schedules,
    next_setpoint,
    target_for_mode,
)
from src.melcloud_client import DeviceInfo, fetch_devices, set_device_state

logger = logging.getLogger(__name__)

_DEFAULT_RANGE = (16.0, 31.0)


@dataclass(frozen=True)
class AutomationConfig:
    """Engine knobs, loaded from ``.env`` (all optional)."""

    enabled: bool = True
    poll_interval_s: int = 60
    adjust_interval_s: int = 900  # 15 min between setpoint nudges per unit
    buffer_c: float = 0.5

    @property
    def fire_grace_s(self) -> int:
        """How long after a schedule's HH:MM it may still fire (catch-up window).

        Two poll cycles or two minutes, whichever is larger — long enough that a
        tick landing just past the minute still fires, short enough that a
        restart hours later does not replay a stale morning schedule.
        """
        return max(120, self.poll_interval_s * 2)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


def load_automation_config() -> AutomationConfig:
    """Read the automation knobs from ``.env`` (graceful defaults)."""
    load_dotenv(override=True)
    return AutomationConfig(
        enabled=_env_bool("HVAC_AUTOMATION_ENABLED", True),
        poll_interval_s=max(10, _env_int("HVAC_POLL_INTERVAL_S", 60)),
        adjust_interval_s=max(60, _env_int("HVAC_ADJUST_INTERVAL_S", 900)),
        buffer_c=max(0.0, _env_float("HVAC_BUFFER_C", 0.5)),
    )


def _mode_range(unit: DeviceInfo) -> tuple[float, float]:
    """The (min, max) setpoint range for the unit's current mode."""
    rng = unit.temp_ranges.get(unit.operation_mode or "")
    if rng and len(rng) == 2:
        return float(rng[0]), float(rng[1])
    return _DEFAULT_RANGE


async def _apply_schedule(unit: DeviceInfo, sched) -> None:
    """Write one schedule entry to one unit."""
    logger.info("⏰ Applying schedule to '%s' (%s, %s)", unit.name, sched.time, sched.id)
    if sched.power is False:
        await set_device_state(unit.unit_id, power=False)
        return
    await set_device_state(
        unit.unit_id,
        power=True,
        operation_mode=sched.operation_mode,
        set_temperature=sched.set_temperature,
        fan_speed=sched.fan_speed,
        vane_vertical_direction=sched.vane_vertical_direction,
        vane_horizontal_direction=sched.vane_horizontal_direction,
    )


def _schedule_due(sched, now: datetime, grace_s: int) -> bool:
    """True if ``now`` falls in ``[HH:MM, HH:MM + grace)`` for today."""
    try:
        hh, mm = (int(p) for p in sched.time.split(":", 1))
    except (ValueError, AttributeError):
        logger.warning("⚠️ Invalid schedule time %r; skipping", sched.time)
        return False
    fire_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now - fire_at).total_seconds()
    return 0 <= delta < grace_s


async def _tick(config: AutomationConfig, state: "_EngineState") -> None:
    """One evaluation pass over every unit (schedules, then rule nudges)."""
    rules = load_rules()
    schedules = load_schedules()
    # Nothing active → don't even hit MELCloud. Keeps a 24/7 idle engine silent
    # on the network until the user enables a rule or at least one schedule
    # entry; disabled entries may still persist for later UI reactivation.
    if not any(rule.enabled for rule in rules.values()) and not any(
        entry.enabled for entries in schedules.values() for entry in entries
    ):
        return

    devices = await fetch_devices()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    monotonic = time.monotonic()

    for unit in devices:
        uid = unit.unit_id

        # --- Schedules: edge-triggered, once per local day per entry. ---
        applied_schedule = False
        for sched in schedules.get(uid, []):
            fire_key = f"{uid}:{sched.id}"
            if sched.enabled and _schedule_due(sched, now, config.fire_grace_s):
                if state.last_fire_day.get(fire_key) != today:
                    try:
                        await _apply_schedule(unit, sched)
                        state.last_fire_day[fire_key] = today
                        state.last_adjust[uid] = monotonic
                        applied_schedule = True
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        logger.warning("⚠️ Schedule apply failed for %s/%s: %s", uid, sched.id, exc)
        if applied_schedule:
            continue  # a schedule just changed power/profile; skip a same-tick nudge

        # --- Rule: dynamic setpoint nudge while the unit is on. ---
        rule = rules.get(uid)
        if rule is None or not rule.enabled or unit.power is not True:
            continue
        target = target_for_mode(rule, unit.operation_mode)
        if target is None:
            continue
        last = state.last_adjust.get(uid, 0.0)
        if monotonic - last < config.adjust_interval_s:
            continue

        tmin, tmax = _mode_range(unit)
        new = next_setpoint(
            operation_mode=unit.operation_mode,
            room_temperature=unit.room_temperature,
            set_temperature=unit.set_temperature,
            target=target,
            buffer=config.buffer_c,
            step=float(unit.temp_step) or 0.5,
            tmin=tmin,
            tmax=tmax,
        )
        # Record the cadence even on a hold so a steady room is re-checked at the
        # adjust interval, not every poll.
        state.last_adjust[uid] = monotonic
        if new is None:
            continue
        try:
            logger.info(
                "🌡️ '%s' room %.1f vs target %.1f → setpoint %.1f→%.1f",
                unit.name, unit.room_temperature, target, unit.set_temperature, new,
            )
            await set_device_state(uid, set_temperature=new)
        except Exception as exc:  # noqa: BLE001 — never kill the loop
            logger.warning("⚠️ Setpoint nudge failed for %s: %s", uid, exc)


@dataclass
class _EngineState:
    """In-memory timing/edge state, keyed by unit id (not persisted)."""

    last_adjust: Dict[str, float]  # monotonic ts of last setpoint write
    last_fire_day: Dict[str, str]  # local date a schedule entry last fired


async def _run(config: AutomationConfig) -> None:
    """Poll → apply schedules → nudge setpoints, until cancelled."""
    state = _EngineState(last_adjust={}, last_fire_day={})
    await run_loop(
        lambda: _tick(config, state),
        config.poll_interval_s,
        logger=logger,
        name="HVAC automation",
        start_msg=(
            "🤖 HVAC automation started (poll %ds, adjust %ds, buffer %.1f°C)"
            % (config.poll_interval_s, config.adjust_interval_s, config.buffer_c)
        ),
        tick_fail_msg="⚠️ HVAC automation tick failed: %s",
    )


def start_automation() -> Optional[asyncio.Task]:
    """Start the automation task if enabled; return it (or ``None`` when off)."""
    config = load_automation_config()
    if not config.enabled:
        logger.info("ℹ️ HVAC automation disabled (HVAC_AUTOMATION_ENABLED) — not steering units")
        return None
    return asyncio.create_task(_run(config), name="hvac-automation")
