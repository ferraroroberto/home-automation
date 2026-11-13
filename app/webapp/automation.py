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
  overshoot). The rule never touches power — see :mod:`src.devices.hvac_automation`.
* **Solar-boost coordinator** — one fleet-wide decision per tick between
  the two per-unit passes above: at most one unit is admitted to (or shed from)
  boost per settle interval, measuring real surplus in between, and the
  admission/shed transition commands the unit's setpoint immediately instead of
  waiting out the throttle. Without it every eligible unit reacted to the same
  surplus reading in the same tick, which oscillates rather than controls.
* **Schedule verification** — after each schedule fire, a background task
  watches the unit's own fan readback, power-cycles a unit that did not
  follow, and alerts once at the retry cap (:mod:`src.devices.hvac_verify`).

The pure decision lives in :mod:`src.devices.hvac_automation`; this module only owns the
loop, the timing/edge state, and the MELCloud reads/writes. Gated by
``HVAC_AUTOMATION_ENABLED`` (``.env``) so the e2e suite and dev runs never drive
real units. Never lets a per-unit error kill the loop (sampler pattern).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from app.webapp import read_snapshot
from app.webapp._env import _env_bool, _env_float, _env_int
from app.webapp._task_loop import run_loop, spawn_background_task
from src import telemetry
from src._schedule_store import DailyFireLatch, FireLatchPolicy, daily_due
from src.devices.hvac_automation import (
    TempRule,
    boosted_target,
    load_boost_config,
    load_rules,
    load_schedules,
    next_boost_admission,
    next_boost_state,
    next_setpoint,
    target_for_mode,
    transition_setpoint,
)
from src.devices.hvac_verify import (
    Request,
    VerifyConfig,
    load_verify_config,
    verify_schedule_fire,
)
from src.devices.melcloud_client import DeviceInfo, fetch_devices, set_device_state
from src.energy.huawei_client import fetch_energy_state
from src.notify_config import build_alarm_notifier, notify_best_effort

logger = logging.getLogger(__name__)

_DEFAULT_RANGE = (16.0, 31.0)


async def _write_unit(unit_id: str, **kwargs: Any) -> DeviceInfo:
    """Write one unit, then mark the ``/api/units`` snapshot dirty.

    Marked even when the write raises, since it may have half-applied; the
    next read refetches rather than serving the pre-write state.
    """
    try:
        return await set_device_state(unit_id, **kwargs)
    finally:
        read_snapshot.invalidate("units")


@dataclass(frozen=True)
class AutomationConfig:
    """Engine knobs, loaded from ``.env`` (all optional)."""

    enabled: bool = True
    poll_interval_s: int = 60
    adjust_interval_s: int = 900  # 15 min between setpoint nudges per unit
    buffer_c: float = 0.5
    # Solar-surplus boost — hysteresis band + debounce, see
    # src.devices.hvac_automation.next_boost_state.
    boost_surplus_on_w: float = 1500.0
    boost_surplus_off_w: float = 500.0
    boost_min_duration_s: int = 1800  # 30 min
    verify: VerifyConfig = field(default_factory=VerifyConfig)

    @property
    def fire_grace_s(self) -> int:
        """How long after a schedule's HH:MM it may still fire (catch-up window).

        Two poll cycles or two minutes, whichever is larger — long enough that a
        tick landing just past the minute still fires, short enough that a
        restart hours later does not replay a stale morning schedule.
        """
        return max(120, self.poll_interval_s * 2)


def load_automation_config() -> AutomationConfig:
    """Read the automation knobs from ``.env`` (graceful defaults)."""
    load_dotenv(override=True)
    return AutomationConfig(
        enabled=_env_bool("HVAC_AUTOMATION_ENABLED", True),
        poll_interval_s=max(10, _env_int("HVAC_POLL_INTERVAL_S", 60)),
        adjust_interval_s=max(60, _env_int("HVAC_ADJUST_INTERVAL_S", 900)),
        buffer_c=max(0.0, _env_float("HVAC_BUFFER_C", 0.5)),
        boost_surplus_on_w=max(0.0, _env_float("HVAC_BOOST_SURPLUS_ON_W", 1500.0)),
        boost_surplus_off_w=max(0.0, _env_float("HVAC_BOOST_SURPLUS_OFF_W", 500.0)),
        boost_min_duration_s=max(0, _env_int("HVAC_BOOST_MIN_DURATION_S", 1800)),
        verify=load_verify_config(),
    )


def _mode_range(unit: DeviceInfo) -> tuple[float, float]:
    """The (min, max) setpoint range for the unit's current mode."""
    rng = unit.temp_ranges.get(unit.operation_mode or "")
    if rng and len(rng) == 2:
        return float(rng[0]), float(rng[1])
    return _DEFAULT_RANGE


def _schedule_profile(sched) -> Dict[str, Any]:
    """The ``set_device_state`` kwargs one schedule entry writes."""
    if sched.power is False:
        return {"power": False}
    return {
        "power": True,
        "operation_mode": sched.operation_mode,
        "set_temperature": sched.set_temperature,
        "fan_speed": sched.fan_speed,
        "vane_vertical_direction": sched.vane_vertical_direction,
        "vane_horizontal_direction": sched.vane_horizontal_direction,
    }


async def _apply_schedule(unit: DeviceInfo, sched) -> None:
    """Write one schedule entry to one unit."""
    logger.info("⏰ Applying schedule to '%s' (%s, %s)", unit.name, sched.time, sched.id)
    await _write_unit(unit.unit_id, **_schedule_profile(sched))


async def _notify_verify_failure(text: str) -> None:
    await notify_best_effort(
        text, label="HVAC schedule verify", notifier_factory=build_alarm_notifier
    )


def _start_verification(
    config: AutomationConfig, state: "_EngineState", unit: DeviceInfo, sched
) -> None:
    """Watch a just-fired schedule in the background, so the tick never waits.

    A newer fire on the same unit replaces any check still running for it:
    the older request is no longer what the unit is meant to be doing.
    """
    if not config.verify.enabled:
        return
    previous = state.verify_tasks.pop(unit.unit_id, None)
    if previous is not None and not previous.done():
        previous.cancel()
    try:
        request = Request(
            unit_id=unit.unit_id,
            unit_name=unit.name,
            sched_time=sched.time,
            profile=_schedule_profile(sched),
        )
    except Exception as exc:  # noqa: BLE001 — a verify setup error never costs the tick
        logger.warning("⚠️ HVAC verify not started for %s: %s", unit.unit_id, exc)
        return
    state.verify_tasks[unit.unit_id] = spawn_background_task(
        verify_schedule_fire(
            request,
            config.verify,
            fetch=fetch_devices,
            write=_write_unit,
            sleep=asyncio.sleep,
            notify=_notify_verify_failure,
            record=telemetry.record_event,
        ),
        name=f"hvac-verify-{unit.unit_id}",
    )


# A failed apply retries on the next poll inside the grace window; only a
# successful apply spends the day.
FIRE_LATCH_POLICY = FireLatchPolicy(
    name="HVAC schedule", file_name="hvac_schedule_last_fire.json", window="grace"
)


def _schedule_due(sched, now: datetime, grace_s: int) -> bool:
    """True if ``now`` falls in ``[HH:MM, HH:MM + grace)`` for today."""
    return daily_due(sched.time, now, grace_s)


def _set_boost(
    state: "_EngineState",
    uid: str,
    unit_name: str,
    active: bool,
    now_monotonic: float,
    surplus_w: Optional[float] = None,
    reason: Optional[str] = None,
    as_of: Optional[str] = None,
) -> None:
    """Update one unit's boost flag; logs to the activity feed on transitions only.

    The single place ``boost_order`` (admission order, for LIFO shedding) and the
    settle-timer stamps are maintained. **Any** change to the boosted set
    restarts the settle timer, including an involuntary clear (a unit powered off
    or its rule disabled): the fleet's draw just changed, so the next surplus
    reading has to be given time to reflect that before anything else moves.
    """
    was_active = state.boost_active.get(uid, False)
    state.boost_active[uid] = active
    if active == was_active:
        return
    state.last_boost_change = now_monotonic
    state.last_boost_as_of = as_of
    if active:
        state.boost_since[uid] = now_monotonic
        if uid not in state.boost_order:
            state.boost_order.append(uid)
        telemetry.record_event(
            "hvac", "boost_start", entity_id=uid, source="hvac_automation",
            payload={"pv_surplus_w": surplus_w, "reason": reason},
        )
        logger.info(
            "☀️ Solar boost started for '%s' (surplus %sW%s)",
            unit_name, surplus_w, f", {reason}" if reason else "",
        )
    else:
        state.boost_since.pop(uid, None)
        if uid in state.boost_order:
            state.boost_order.remove(uid)
        telemetry.record_event(
            "hvac", "boost_stop", entity_id=uid, source="hvac_automation",
            payload={"pv_surplus_w": surplus_w, "reason": reason},
        )
        logger.info(
            "☀️ Solar boost stopped for '%s'%s", unit_name, f" ({reason})" if reason else ""
        )


@dataclass
class _Steerable:
    """A unit that passed this tick's eligibility checks (on, steerable, ruled)."""

    unit: DeviceInfo
    rule: TempRule
    target: float


def _log_boost_hold(
    state: "_EngineState",
    decision,
    surplus_w: Optional[float],
    by_uid: Dict[str, _Steerable],
) -> None:
    """Record a ``held_margin`` episode once, not once per tick.

    A hold is not a state transition, and the engine re-evaluates every
    ``poll_interval_s`` — logging it per evaluation would bury the activity feed
    under one event a minute. Edge-triggered: one event per contiguous hold
    episode, re-armed as soon as any other decision is taken (or a different
    candidate becomes the blocked one).
    """
    if decision.reason != "held_margin" or not decision.held:
        state.boost_hold_logged = None
        return
    uid = decision.held[0]
    if state.boost_hold_logged == uid:
        return
    state.boost_hold_logged = uid
    telemetry.record_event(
        "hvac", "boost_hold", entity_id=uid, source="hvac_automation",
        payload={"pv_surplus_w": surplus_w, "reason": "held_margin"},
    )
    item = by_uid.get(uid)
    logger.info(
        "☀️ Solar boost held for '%s' — surplus %sW is under the entry threshold "
        "plus admission margin",
        item.unit.name if item else uid, surplus_w,
    )


async def _write_transition_setpoint(
    config: AutomationConfig,
    state: "_EngineState",
    item: _Steerable,
    *,
    boosted: bool,
    monotonic: float,
) -> None:
    """Command the unit's setpoint *now*, on a boost admission/shed.

    Bypasses the ``adjust_interval_s`` throttle for this one write and computes
    the setpoint straight from the (un)boosted target instead of one step from
    the current one — see :func:`src.devices.hvac_automation.transition_setpoint` for
    why the sequencer is unsound without it. Still clamped by ``_mode_range``,
    and still a **setpoint write only**: the coordinator never adds a power
    path, so the compressor is never cycled.
    """
    unit = item.unit
    target = boosted_target(
        operation_mode=unit.operation_mode,
        target=item.target,
        boost_offset_c=item.rule.boost_offset_c,
        is_boosting=boosted,
    )
    tmin, tmax = _mode_range(unit)
    new = transition_setpoint(
        operation_mode=unit.operation_mode,
        room_temperature=unit.room_temperature,
        set_temperature=unit.set_temperature,
        target=target,
        buffer=config.buffer_c,
        tmin=tmin,
        tmax=tmax,
    )
    # Restart the steady-state cadence from the transition either way: a hold
    # means the unit is already where the transition wants it, so re-checking it
    # on the very next poll would be noise.
    state.last_adjust[unit.unit_id] = monotonic
    if new is None:
        return
    try:
        logger.info(
            "☀️ '%s' %s → immediate setpoint %.1f→%.1f (target %.1f)",
            unit.name, "boost admitted" if boosted else "boost shed",
            unit.set_temperature, new, target,
        )
        await _write_unit(unit.unit_id, set_temperature=new)
    except Exception as exc:  # noqa: BLE001 — never kill the loop
        logger.warning("⚠️ Boost transition write failed for %s: %s", unit.unit_id, exc)


async def _coordinate_boost(
    config: AutomationConfig,
    state: "_EngineState",
    steerable: List[_Steerable],
    surplus_w: Optional[float],
    as_of: Optional[str],
    monotonic: float,
) -> None:
    """Sequence boost across the fleet: at most one admission or shed per tick.

    Per-unit hysteresis (:func:`next_boost_state`) only says whether a unit
    *wants* boost; :func:`next_boost_admission` decides who actually gets it now.
    The coordinator knobs are re-read here every tick so an Energy-tab edit is
    live without a tray restart.
    """
    by_uid: Dict[str, _Steerable] = {}
    wants: Dict[str, bool] = {}
    for item in steerable:
        if not item.rule.boost_enabled:
            continue
        uid = item.unit.unit_id
        by_uid[uid] = item
        wants[uid] = next_boost_state(
            currently_boosting=state.boost_active.get(uid, False),
            boosting_since=state.boost_since.get(uid),
            pv_surplus_w=surplus_w,
            now_monotonic=monotonic,
            surplus_on_w=config.boost_surplus_on_w,
            surplus_off_w=config.boost_surplus_off_w,
            min_duration_s=config.boost_min_duration_s,
        )

    # Nobody opted in and nothing is boosted → no decision to make. Keeps a
    # boost-free install from re-reading the coordinator file every poll.
    if not wants and not state.boost_order:
        state.boost_hold_logged = None
        return

    coord = load_boost_config()
    # Logged from here rather than from the loop's start message —
    # this is the first place the file is actually read, and a read here is
    # retried by the tick loop instead of killing the task on the way in.
    if state.boost_settle_logged != coord.settle_interval_s:
        state.boost_settle_logged = coord.settle_interval_s
        logger.info("🤖 HVAC boost settle interval: %ds", coord.settle_interval_s)
    decision = next_boost_admission(
        wants_boost=wants,
        # A unit that vanished from MELCloud's list this tick is not a unit we
        # can still steer, so it never holds up the queue.
        admitted_order=[uid for uid in state.boost_order if uid in by_uid],
        pv_surplus_w=surplus_w,
        now_monotonic=monotonic,
        last_change_monotonic=state.last_boost_change,
        last_change_as_of=state.last_boost_as_of,
        energy_as_of=as_of,
        surplus_on_w=config.boost_surplus_on_w,
        config=coord,
    )

    for uid in decision.shed:
        item = by_uid.get(uid)
        if item is None:
            continue
        _set_boost(
            state, uid, item.unit.name, False, monotonic,
            surplus_w=surplus_w, reason=decision.reason, as_of=as_of,
        )
        await _write_transition_setpoint(
            config, state, item, boosted=False, monotonic=monotonic
        )

    if decision.admit is not None:
        item = by_uid[decision.admit]
        _set_boost(
            state, decision.admit, item.unit.name, True, monotonic,
            surplus_w=surplus_w, reason=decision.reason, as_of=as_of,
        )
        await _write_transition_setpoint(
            config, state, item, boosted=True, monotonic=monotonic
        )

    _log_boost_hold(state, decision, surplus_w, by_uid)


async def _tick(config: AutomationConfig, state: "_EngineState") -> None:
    """One evaluation pass: schedules, then the fleet boost sequencer, then nudges."""
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

    # One shared FusionSolar read per tick (already 60s-cached upstream), only
    # when at least one unit has opted in.
    energy = None
    if any(rule.boost_enabled for rule in rules.values()):
        energy = await fetch_energy_state()
    surplus = energy.pv_surplus_w if energy is not None else None
    as_of = energy.as_of if energy is not None else None

    # --- Pass 1: schedules, then who is eligible to be steered at all. ---
    steerable: List[_Steerable] = []
    for unit in devices:
        uid = unit.unit_id

        # --- Schedules: edge-triggered, once per local day per entry. ---
        applied_schedule = False
        for sched in schedules.get(uid, []):
            if not sched.enabled:
                continue
            fire_key = f"{uid}:{sched.id}"
            due = _schedule_due(sched, now, config.fire_grace_s)
            state.latch.note_window(fire_key, today, due)
            if not due or state.latch.fired(fire_key, today):
                continue
            try:
                await state.latch.fire_once(
                    fire_key, today, lambda u=unit, s=sched: _apply_schedule(u, s)
                )
                state.last_adjust[uid] = monotonic
                applied_schedule = True
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                logger.warning("⚠️ Schedule apply failed for %s/%s: %s", uid, sched.id, exc)
                continue
            _start_verification(config, state, unit, sched)
        if applied_schedule:
            continue  # a schedule just changed power/profile; skip a same-tick nudge

        rule = rules.get(uid)
        if rule is None or not rule.enabled or unit.power is not True:
            if state.boost_active.get(uid):
                _set_boost(
                    state, uid, unit.name, False, monotonic,
                    surplus_w=surplus, reason="rule_inactive", as_of=as_of,
                )
            continue
        target = target_for_mode(rule, unit.operation_mode)
        if target is None:
            if state.boost_active.get(uid):
                _set_boost(
                    state, uid, unit.name, False, monotonic,
                    surplus_w=surplus, reason="no_target", as_of=as_of,
                )
            continue
        if not rule.boost_enabled and state.boost_active.get(uid):
            _set_boost(
                state, uid, unit.name, False, monotonic,
                surplus_w=surplus, reason="boost_disabled", as_of=as_of,
            )
        steerable.append(_Steerable(unit=unit, rule=rule, target=target))

    # --- Pass 2: one fleet-wide boost decision. Evaluated every tick,
    # deliberately not gated by the adjust throttle below, so hysteresis,
    # min-duration and settle timing all track real elapsed time. ---
    await _coordinate_boost(config, state, steerable, surplus, as_of, monotonic)

    # --- Pass 3: the steady-state steering nudge, unchanged. An
    # already-boosted unit is back on the gradual one-step-per-interval law here;
    # only the admission/shed transition itself jumps (see _coordinate_boost). ---
    for item in steerable:
        await _nudge_setpoint(config, state, item, monotonic)


async def _nudge_setpoint(
    config: AutomationConfig, state: "_EngineState", item: _Steerable, monotonic: float
) -> None:
    """Move one steerable unit's setpoint a step toward its (boosted) target."""
    unit, rule = item.unit, item.rule
    uid = unit.unit_id
    last = state.last_adjust.get(uid, 0.0)
    if monotonic - last < config.adjust_interval_s:
        return

    is_boosting = state.boost_active.get(uid, False)
    effective_target = boosted_target(
        operation_mode=unit.operation_mode,
        target=item.target,
        boost_offset_c=rule.boost_offset_c,
        is_boosting=is_boosting,
    )

    tmin, tmax = _mode_range(unit)
    new = next_setpoint(
        operation_mode=unit.operation_mode,
        room_temperature=unit.room_temperature,
        set_temperature=unit.set_temperature,
        target=effective_target,
        buffer=config.buffer_c,
        step=float(unit.temp_step) or 0.5,
        tmin=tmin,
        tmax=tmax,
    )
    # Record the cadence even on a hold so a steady room is re-checked at the
    # adjust interval, not every poll.
    state.last_adjust[uid] = monotonic
    if new is None:
        return
    try:
        logger.info(
            "🌡️ '%s' room %.1f vs target %.1f%s → setpoint %.1f→%.1f",
            unit.name, unit.room_temperature, effective_target,
            " (boosted)" if is_boosting else "", unit.set_temperature, new,
        )
        await _write_unit(uid, set_temperature=new)
    except Exception as exc:  # noqa: BLE001 — never kill the loop
        logger.warning("⚠️ Setpoint nudge failed for %s: %s", uid, exc)


@dataclass
class _EngineState:
    """Timing/edge state keyed by unit id; only the schedule latch is persisted."""

    last_adjust: Dict[str, float] = field(default_factory=dict)  # monotonic ts of last setpoint write
    boost_active: Dict[str, bool] = field(default_factory=dict)  # is this unit currently solar-boosted
    boost_since: Dict[str, float] = field(default_factory=dict)  # monotonic ts the current boost started
    # "unit:entry" -> local date the schedule entry last fired.
    latch: DailyFireLatch = field(default_factory=lambda: DailyFireLatch(FIRE_LATCH_POLICY))
    # Coordinator state. boost_order is the *admission order* — an explicit
    # ordered record, because boost_since is a dict of transition stamps and is
    # the wrong shape to reason about LIFO shedding with.
    boost_order: List[str] = field(default_factory=list)
    last_boost_change: Optional[float] = None  # monotonic ts of the last admit/shed
    last_boost_as_of: Optional[str] = None  # solar bucket that change was made against
    boost_hold_logged: Optional[str] = None  # unit whose hold episode is already logged
    boost_settle_logged: Optional[int] = None  # settle interval already announced
    # unit id -> the background check watching that unit's latest schedule fire
    verify_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)


# The running engine's state, for the API layer to read live "is this unit
# currently boosted" without a second event loop/store. None when the
# engine is disabled or hasn't started yet — read via get_boost_active().
_ENGINE_STATE: Optional[_EngineState] = None


def get_boost_active(unit_id: str) -> bool:
    """True if the running engine currently has this unit solar-boosted."""
    return bool(_ENGINE_STATE and _ENGINE_STATE.boost_active.get(unit_id, False))


async def _run(config: AutomationConfig) -> None:
    """Poll → apply schedules → nudge setpoints, until cancelled."""
    global _ENGINE_STATE
    state = _EngineState()
    _ENGINE_STATE = state
    await run_loop(
        lambda: _tick(config, state),
        config.poll_interval_s,
        logger=logger,
        name="HVAC automation",
        # Deliberately no `load_boost_config()` here: this argument is
        # evaluated *before* `run_loop` is entered, so the `tick_fail_msg` below
        # cannot protect it — an unreadable `config/hvac_boost.json` at boot would
        # kill the whole task permanently, with no retry and no restart. The settle
        # interval is logged from the first tick that actually reads it instead.
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
