"""On-but-idle detection: the detector, the one power cycle + alert, and the engine wiring.

Every seam is faked: reads come from a fixed unit, writes and alerts are
recorded, and sleeps return at once. Nothing reaches MELCloud, a real unit or
Telegram.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Dict, List

import pytest

import app.webapp.automation as hvac
from src.devices.hvac_automation import TempRule
from src.devices.hvac_idle import (
    IdleConfig,
    IdleTracker,
    load_idle_config,
    looks_idle,
    recover_idle_unit,
)
from src.devices.hvac_verify import Verdict, VerifyConfig
from src.devices.melcloud_client import DeviceInfo

UID = "u1"
CFG = IdleConfig()  # 20 min window, 1 °C gap, 0.5 °C progress, 1 h cooldown
_real_sleep = asyncio.sleep
_real_monotonic = time.monotonic
_UPTIME_S = 86_400.0  # far past the 15 min adjust interval


def _unit(**kw: Any) -> DeviceInfo:
    base = dict(
        unit_id=UID, name="Unit A", building="Home", power=True, operation_mode="Heat",
        room_temperature=18.5, set_temperature=23.0, fan_speed="Two", actual_fan_speed="Off",
    )
    base.update(kw)
    return DeviceInfo(**base)


STUCK = _unit()  # reads on, its own fan off, room well short of the setpoint
RUNNING = _unit(actual_fan_speed="Two")


def _feed(tracker: IdleTracker, unit: DeviceInfo, minutes: List[float]) -> List[bool]:
    return [tracker.observe(unit, m * 60.0, CFG) for m in minutes]


# --------------------------------------------------------------------- detector
def test_a_stuck_unit_is_flagged_once_after_the_window() -> None:
    tracker = IdleTracker()
    assert _feed(tracker, STUCK, [0, 10, 19]) == [False, False, False]
    assert not tracker.is_flagged(UID)
    assert _feed(tracker, STUCK, [20, 21, 60, 120]) == [True, False, False, False]
    assert tracker.is_flagged(UID)


def test_the_flag_clears_once_the_fan_turns() -> None:
    tracker = IdleTracker()
    _feed(tracker, STUCK, [0, 20])
    tracker.observe(RUNNING, 21 * 60.0, CFG)
    assert not tracker.is_flagged(UID)


def test_a_healthy_unit_reaching_the_setpoint_is_never_flagged() -> None:
    tracker = IdleTracker()
    warming = [_unit(actual_fan_speed="Two", room_temperature=19.0 + i) for i in range(4)]
    for i, unit in enumerate(warming):
        assert tracker.observe(unit, i * 600.0, CFG) is False
    # At the setpoint the unit rests its fan; that is not idle.
    at_setpoint = _unit(room_temperature=23.0)
    assert _feed(tracker, at_setpoint, [40, 80, 200]) == [False, False, False]
    assert not tracker.is_flagged(UID)


def test_a_fan_resting_within_the_gap_is_not_idle() -> None:
    assert not looks_idle(_unit(room_temperature=22.5), CFG)
    assert looks_idle(_unit(room_temperature=22.0), CFG)
    assert looks_idle(_unit(operation_mode="Cool", room_temperature=27.0, set_temperature=24.0), CFG)
    assert not looks_idle(_unit(operation_mode="Cool", room_temperature=23.0, set_temperature=24.0), CFG)


@pytest.mark.parametrize("unit", [
    _unit(power=False), _unit(operation_mode="Automatic"), _unit(operation_mode="Fan"),
    _unit(operation_mode="Dry"), _unit(actual_fan_speed=None), _unit(room_temperature=None),
    _unit(reachable=False),
])
def test_states_that_are_not_stuck(unit: DeviceInfo) -> None:
    assert not looks_idle(unit, CFG)


def test_a_defrost_length_stop_is_not_flagged() -> None:
    tracker = IdleTracker()
    _feed(tracker, STUCK, [0, 8])
    tracker.observe(RUNNING, 9 * 60.0, CFG)  # fan back after a defrost
    assert _feed(tracker, STUCK, [10, 25]) == [False, False]  # the window restarted


def test_a_room_closing_in_restarts_the_window() -> None:
    tracker = IdleTracker()
    tracker.observe(STUCK, 0.0, CFG)
    tracker.observe(_unit(room_temperature=19.0), 15 * 60.0, CFG)  # +0.5 °C: working
    assert tracker.observe(_unit(room_temperature=19.0), 25 * 60.0, CFG) is False
    assert tracker.observe(_unit(room_temperature=19.0), 35 * 60.0, CFG) is True


def test_a_relapse_inside_the_cooldown_is_flagged_but_not_cycled() -> None:
    tracker = IdleTracker()
    assert _feed(tracker, STUCK, [0, 20]) == [False, True]
    tracker.observe(RUNNING, 25 * 60.0, CFG)
    assert _feed(tracker, STUCK, [30, 50]) == [False, False]
    assert tracker.is_flagged(UID)  # the rule still leaves it alone
    tracker.observe(RUNNING, 55 * 60.0, CFG)
    assert _feed(tracker, STUCK, [90, 110]) == [False, True]  # past the cooldown


def test_load_idle_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HVAC_IDLE_ENABLED", "false")
    monkeypatch.setenv("HVAC_IDLE_WINDOW_S", "900")
    monkeypatch.setenv("HVAC_IDLE_MIN_GAP_C", "1.5")
    monkeypatch.setenv("HVAC_IDLE_COOLDOWN_S", "7200")
    cfg = load_idle_config()
    assert (cfg.enabled, cfg.window_s, cfg.min_gap_c, cfg.cooldown_s) == (False, 900.0, 1.5, 7200.0)


# ----------------------------------------------------------- cycle, then alert
class World:
    def __init__(self, reads: DeviceInfo) -> None:
        self.unit = reads
        self.writes: List[Dict[str, Any]] = []
        self.alerts: List[str] = []
        self.events: List[str] = []

    async def fetch(self) -> List[DeviceInfo]:
        return [self.unit]

    async def write(self, unit_id: str, **kw: Any) -> None:
        self.writes.append({"unit_id": unit_id, **kw})

    async def sleep(self, _s: float) -> None:
        return None

    async def notify(self, text: str) -> None:
        self.alerts.append(text)

    def record(self, _domain: str, _event: str, **kw: Any) -> None:
        self.events.append(kw["outcome"])

    def run(self):
        return asyncio.run(recover_idle_unit(
            STUCK, VerifyConfig(), fetch=self.fetch, write=self.write, sleep=self.sleep,
            notify=self.notify, record=self.record,
        ))


def test_a_stuck_unit_is_cycled_once_then_alerted() -> None:
    world = World(STUCK)
    result = world.run()
    assert result.verdict is Verdict.NOT_RUNNING and result.alerted
    assert world.writes == [
        {"unit_id": UID, "power": False},
        {"unit_id": UID, "power": True, "operation_mode": "Heat", "set_temperature": 23.0},
    ]
    assert len(world.alerts) == 2 and "power-cycling it now" in world.alerts[0]
    assert "Unit A" in world.alerts[1] and "still idle" in world.alerts[1]
    assert world.events == ["cycle", "failed"]


def test_a_cycle_that_starts_the_unit_tells_the_owner_it_recovered() -> None:
    world = World(RUNNING)
    result = world.run()
    assert result.verdict is Verdict.CONFIRMED and result.alerted
    assert len(world.writes) == 2 and len(world.alerts) == 2
    assert "running now, after one power cycle" in world.alerts[1]
    assert world.events == ["cycle", "recovered"]


def test_no_readback_alerts_as_could_not_confirm() -> None:
    world = World(STUCK)

    async def no_units() -> List[DeviceInfo]:
        return []

    world.fetch = no_units  # type: ignore[method-assign]
    result = world.run()
    assert result.verdict is Verdict.READ_FAILED
    assert len(world.alerts) == 2 and "could not confirm" in world.alerts[1]


# --------------------------------------------------------------- engine wiring
@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The real tick over one stuck unit with a heat rule; every seam faked."""
    world: Dict[str, Any] = {"unit": STUCK, "writes": [], "alerts": []}

    async def fake_fetch() -> List[DeviceInfo]:
        return [world["unit"]]

    async def fake_set(unit_id: str, **kw: Any) -> None:
        world["writes"].append(kw)

    async def fake_notify(text: str) -> None:
        world["alerts"].append(text)

    async def no_boost(*_a: Any, **_k: Any) -> None:
        return None

    async def instant_sleep(_s: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(hvac, "load_rules", lambda: {UID: TempRule(enabled=True, heat_target=23.0)})
    monkeypatch.setattr(hvac, "load_schedules", lambda: {})
    monkeypatch.setattr(hvac, "fetch_devices", fake_fetch)
    monkeypatch.setattr(hvac, "set_device_state", fake_set)
    monkeypatch.setattr(hvac, "_coordinate_boost", no_boost)
    monkeypatch.setattr(hvac, "_notify_idle_unit", fake_notify)
    monkeypatch.setattr(hvac.asyncio, "sleep", instant_sleep)
    # `last_adjust` defaults to 0.0, so the nudge throttle reads "adjusted
    # `monotonic()` seconds ago". On Windows that is host uptime: a freshly
    # booted runner sits inside the adjust interval and never nudges. Shift the
    # clock (still monotonic, so asyncio timers are unaffected) to read as a
    # host that has been up a day, identical on every machine.
    monkeypatch.setattr(time, "monotonic", lambda: _real_monotonic() + _UPTIME_S)
    monkeypatch.setattr(hvac.telemetry, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(hvac.read_snapshot, "invalidate", lambda _k: None)
    return world


def _idle_since_long_ago(state: Any, unit: DeviceInfo) -> None:
    """Open the unit's idle episode a full window before now."""
    state.idle.observe(unit, time.monotonic() - CFG.window_s - 60, CFG)


def test_the_rule_raises_the_setpoint_of_a_unit_not_yet_flagged(engine) -> None:
    state = hvac._EngineState()
    asyncio.run(hvac._tick(hvac.AutomationConfig(), state))
    assert engine["writes"] == [{"set_temperature": 23.5}]
    assert state.idle_tasks == {}


def test_the_engine_cycles_a_stuck_unit_once_alerts_and_the_rule_skips_it(engine) -> None:
    state = hvac._EngineState()
    _idle_since_long_ago(state, STUCK)

    async def scenario() -> None:
        config = hvac.AutomationConfig()
        await hvac._tick(config, state)
        assert state.idle.is_flagged(UID)
        task = state.idle_tasks[UID]
        await asyncio.wait_for(task, timeout=2)
        state.last_adjust.clear()  # the adjust interval has long passed
        await hvac._tick(config, state)  # still idle: no second cycle, no nudge
        assert state.idle_tasks[UID] is task

    asyncio.run(scenario())
    assert [w.get("power") for w in engine["writes"]] == [False, True]
    assert not any("set_temperature" in w and "power" not in w for w in engine["writes"])
    assert len(engine["alerts"]) == 2  # cycle started, then still idle
    assert hvac.get_idle_flagged(UID) is False  # no running engine in a test


def test_the_cycles_own_off_read_never_unflags_the_unit(engine) -> None:
    """A tick landing in the power cycle's off gap must not open a second episode."""
    state = hvac._EngineState()
    _idle_since_long_ago(state, STUCK)

    async def scenario() -> None:
        config = hvac.AutomationConfig()
        await hvac._tick(config, state)
        task = state.idle_tasks[UID]
        engine["unit"] = replace(STUCK, power=False)  # mid-cycle read
        await hvac._tick(config, state)
        assert state.idle.is_flagged(UID)
        engine["unit"] = STUCK
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert state.idle.is_flagged(UID)


def test_a_unit_that_starts_again_is_steered_again(engine) -> None:
    state = hvac._EngineState()
    _idle_since_long_ago(state, STUCK)
    engine["unit"] = replace(RUNNING, room_temperature=19.0)

    asyncio.run(hvac._tick(hvac.AutomationConfig(), state))
    assert not state.idle.is_flagged(UID)
    assert engine["writes"] == [{"set_temperature": 23.5}]


def test_idle_detection_waits_for_a_running_schedule_check(engine) -> None:
    state = hvac._EngineState()
    _idle_since_long_ago(state, STUCK)

    async def scenario() -> None:
        pending = asyncio.ensure_future(asyncio.Event().wait())
        state.verify_tasks[UID] = pending
        await hvac._tick(hvac.AutomationConfig(), state)
        pending.cancel()

    asyncio.run(scenario())
    assert state.idle_tasks == {} and not state.idle.is_flagged(UID)


def test_idle_detection_off_never_flags(engine) -> None:
    state = hvac._EngineState()
    _idle_since_long_ago(state, STUCK)
    asyncio.run(hvac._tick(hvac.AutomationConfig(idle=IdleConfig(enabled=False)), state))
    assert state.idle_tasks == {}
    assert engine["writes"] == [{"set_temperature": 23.5}]
