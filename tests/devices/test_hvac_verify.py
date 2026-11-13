"""Schedule-fire verification: readback verdicts, power-cycle retries, one alert.

Every seam is faked: MELCloud reads come from a scripted list, writes are
recorded, the clock only adds up the requested sleeps, and the notifier and
activity-feed recorder collect what they are given. Nothing reaches a real unit
or Telegram.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Union

import pytest

import app.webapp.automation as hvac
from src.devices.hvac_verify import (
    Request,
    Verdict,
    VerifyConfig,
    classify,
    load_verify_config,
    verify_schedule_fire,
)
from src.devices.melcloud_client import DeviceInfo

UID = "u1"
ON_REQ = Request(
    unit_id=UID, unit_name="Lounge", sched_time="08:00",
    profile={"power": True, "operation_mode": "Heat", "set_temperature": 23.0},
)
OFF_REQ = Request(unit_id=UID, unit_name="Lounge", sched_time="23:00", profile={"power": False})


def _unit(**kw: Any) -> DeviceInfo:
    base = dict(
        unit_id=UID, name="Lounge", building="Home", power=True, operation_mode="Heat",
        room_temperature=18.5, set_temperature=23.0, fan_speed="Two", actual_fan_speed="Two",
    )
    base.update(kw)
    return DeviceInfo(**base)


RUNNING = _unit()
STUCK = _unit(actual_fan_speed="Off")  # reads on, fan never turns — the reported failure
Read = Union[DeviceInfo, Exception, None]


class World:
    """Scripted MELCloud plus recording fakes for every other seam."""

    def __init__(self, reads: List[Read], repeat_last: bool = True) -> None:
        self.reads = list(reads)
        self.repeat_last = repeat_last
        self.writes: List[Dict[str, Any]] = []
        self.slept: List[float] = []
        self.alerts: List[str] = []
        self.events: List[Dict[str, Any]] = []

    @property
    def clock(self) -> float:
        return sum(self.slept)

    async def fetch(self) -> List[DeviceInfo]:
        item = self.reads.pop(0) if len(self.reads) > 1 or not self.repeat_last else self.reads[0]
        if isinstance(item, Exception):
            raise item
        return [] if item is None else [item]

    async def write(self, unit_id: str, **kwargs: Any) -> None:
        self.writes.append({"unit_id": unit_id, "at": self.clock, **kwargs})

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def notify(self, text: str) -> None:
        self.alerts.append(text)

    def record(self, domain: str, event_type: str, **kw: Any) -> None:
        self.events.append({"domain": domain, "event_type": event_type, **kw})

    def run(self, request: Request, config: Optional[VerifyConfig] = None):
        return asyncio.run(
            verify_schedule_fire(
                request, config or VerifyConfig(),
                fetch=self.fetch, write=self.write, sleep=self.sleep,
                notify=self.notify, record=self.record,
            )
        )

    def outcomes(self) -> List[str]:
        return [e["outcome"] for e in self.events]


# ------------------------------------------------------------ classify
@pytest.mark.parametrize(
    ("request_", "unit", "expected"),
    [
        (ON_REQ, RUNNING, Verdict.CONFIRMED),
        (ON_REQ, _unit(actual_fan_speed="One"), Verdict.CONFIRMED),  # actual differs from set
        (ON_REQ, STUCK, Verdict.NOT_RUNNING),
        (ON_REQ, _unit(actual_fan_speed="Off", room_temperature=23.5), Verdict.SATISFIED),
        (ON_REQ, _unit(power=False, actual_fan_speed="Off"), Verdict.SUPERSEDED),
        (ON_REQ, _unit(operation_mode="Cool"), Verdict.SUPERSEDED),
        (ON_REQ, _unit(actual_fan_speed=None), Verdict.READ_FAILED),
        (ON_REQ, _unit(power=None), Verdict.READ_FAILED),
        (ON_REQ, None, Verdict.READ_FAILED),
        (OFF_REQ, _unit(power=False, actual_fan_speed="Off"), Verdict.CONFIRMED),
        (OFF_REQ, _unit(power=False, actual_fan_speed="One"), Verdict.STILL_RUNNING),
        (OFF_REQ, RUNNING, Verdict.SUPERSEDED),
    ],
)
def test_classify(request_: Request, unit: Optional[DeviceInfo], expected: Verdict) -> None:
    assert classify(request_, unit) is expected


@pytest.mark.parametrize(
    ("mode", "room", "setpoint", "expected"),
    [
        ("Cool", 24.0, 25.0, Verdict.SATISFIED),
        ("Cool", 27.0, 25.0, Verdict.NOT_RUNNING),
        ("Dry", 24.0, 25.0, Verdict.SATISFIED),
        ("Automatic", 22.5, 22.0, Verdict.SATISFIED),
        ("Automatic", 18.0, 22.0, Verdict.NOT_RUNNING),
        ("Fan", 22.0, 22.0, Verdict.NOT_RUNNING),
    ],
)
def test_classify_thermo_satisfied_per_mode(mode, room, setpoint, expected) -> None:
    req = replace(ON_REQ, profile={"power": True, "operation_mode": mode})
    unit = _unit(operation_mode=mode, room_temperature=room, set_temperature=setpoint,
                 actual_fan_speed="Off")
    assert classify(req, unit) is expected


# ------------------------------------------------------------ the run
def test_a_unit_that_follows_gets_no_retry_and_no_alert() -> None:
    world = World([RUNNING])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.CONFIRMED
    assert world.writes == [] and world.alerts == []
    assert world.slept == [180.0]  # decided on the first read
    assert world.outcomes() == ["confirmed"]


def test_a_late_heat_start_inside_the_window_is_confirmed() -> None:
    world = World([STUCK, STUCK, RUNNING])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.CONFIRMED
    assert world.writes == [] and world.alerts == []
    assert world.slept == [180.0, 120.0, 180.0]  # reads at +3, +5 and +8 min


def test_a_stuck_unit_is_power_cycled_with_backoff_then_alerted_once() -> None:
    world = World([STUCK])
    result = world.run(ON_REQ)

    assert result.verdict is Verdict.NOT_RUNNING
    assert result.attempts == 3
    # Each retry is off, a pause, then the full profile — never the schedule re-fired.
    assert [w.get("power") for w in world.writes] == [False, True, False, True]
    assert world.writes[1]["operation_mode"] == "Heat"
    assert world.writes[1]["set_temperature"] == 23.0
    # window(+3,+5,+8) · 1 min backoff · cycle pause · window · 2 min backoff · pause · window
    assert world.slept == [
        180.0, 120.0, 180.0, 60.0, 10.0, 180.0, 120.0, 180.0,
        120.0, 10.0, 180.0, 120.0, 180.0,
    ]
    assert len(world.alerts) == 1
    alert = world.alerts[0]
    assert "Lounge" in alert and "08:00" in alert
    assert "on Heat 23.0°C" in alert and "fan Off" in alert and "room 18.5°C" in alert
    assert world.outcomes() == ["retry", "retry", "failed"]
    assert result.alerted


def test_retries_stop_at_the_configured_cap() -> None:
    world = World([STUCK])
    result = world.run(ON_REQ, VerifyConfig(retries=0))
    assert world.writes == []
    assert result.attempts == 1
    assert len(world.alerts) == 1


def test_a_cycle_that_works_ends_the_run_without_an_alert() -> None:
    # first window stuck (3 reads), pre-retry read stuck, then running after the cycle
    world = World([STUCK, STUCK, STUCK, STUCK, RUNNING])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.CONFIRMED
    assert result.attempts == 2
    assert world.alerts == []
    assert world.outcomes() == ["retry", "confirmed"]


def test_a_manual_change_during_the_backoff_is_respected_not_undone() -> None:
    switched_off = _unit(power=False, actual_fan_speed="Off")
    world = World([STUCK, STUCK, STUCK, switched_off])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.SUPERSEDED
    assert world.writes == [] and world.alerts == []
    assert world.outcomes() == ["superseded"]


def test_a_satisfied_room_is_unconfirmed_with_no_retry_and_no_alert() -> None:
    world = World([_unit(actual_fan_speed="Off", room_temperature=23.5)])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.SATISFIED
    assert world.writes == [] and world.alerts == []
    assert world.outcomes() == ["unconfirmed"]


def test_failed_reads_never_resend_and_alert_as_could_not_verify() -> None:
    world = World([RuntimeError("cloud down")])
    result = world.run(ON_REQ)
    assert result.verdict is Verdict.READ_FAILED
    assert world.writes == []
    assert len(world.alerts) == 1
    assert "could not verify" in world.alerts[0]
    assert "did not take effect" not in world.alerts[0]
    assert world.outcomes() == ["unverified"]


def test_one_good_read_among_failures_still_confirms() -> None:
    world = World([RuntimeError("blip"), RUNNING])
    assert world.run(ON_REQ).verdict is Verdict.CONFIRMED
    assert world.alerts == []


def test_power_off_still_running_is_resent_plainly_then_alerted() -> None:
    world = World([_unit(power=False, actual_fan_speed="Two")])
    result = world.run(OFF_REQ)
    assert result.verdict is Verdict.STILL_RUNNING
    assert [w["power"] for w in world.writes] == [False, False]  # no on-cycle for an off
    assert 10.0 not in world.slept
    assert len(world.alerts) == 1 and "23:00" in world.alerts[0]


def test_power_off_that_stops_is_confirmed() -> None:
    world = World([_unit(power=False, actual_fan_speed="One"), _unit(power=False, actual_fan_speed="Off")])
    assert world.run(OFF_REQ).verdict is Verdict.CONFIRMED
    assert world.writes == [] and world.alerts == []


def test_a_failing_notifier_never_raises() -> None:
    world = World([STUCK])

    async def broken(_text: str) -> None:
        raise RuntimeError("telegram down")

    world.notify = broken  # type: ignore[method-assign]
    result = world.run(ON_REQ, VerifyConfig(retries=0))
    assert result.alerted is False


def test_load_verify_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HVAC_VERIFY_READS_S", "300, 60")
    monkeypatch.setenv("HVAC_VERIFY_RETRIES", "1")
    monkeypatch.setenv("HVAC_VERIFY_BACKOFF_S", "bad")
    monkeypatch.setenv("HVAC_VERIFY_ENABLED", "false")
    cfg = load_verify_config()
    assert cfg.read_offsets_s == (60.0, 300.0)
    assert cfg.retries == 1
    assert cfg.backoff_s == VerifyConfig.backoff_s
    assert cfg.enabled is False


# ------------------------------------------------------------ engine wiring
class _Clock(datetime):
    current = datetime(2026, 6, 22, 8, 0, 10)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def test_the_tick_returns_while_the_verification_is_still_pending(monkeypatch) -> None:
    """The schedule fires, the tick finishes, and the check is still waiting in the background."""
    sched = SimpleNamespace(id="s1", time="08:00", enabled=True, power=True,
                            operation_mode="Heat", set_temperature=23.0, fan_speed=None,
                            vane_vertical_direction=None, vane_horizontal_direction=None)
    writes: List[Dict[str, Any]] = []
    gate = asyncio.Event()

    async def fake_set_device_state(unit_id: str, **kw: Any) -> None:
        writes.append(kw)

    async def fake_fetch() -> List[DeviceInfo]:
        return [_unit(power=False, actual_fan_speed="Off")]

    async def blocked_sleep(_s: float) -> None:
        await gate.wait()

    async def no_boost(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(hvac, "datetime", _Clock)
    monkeypatch.setattr(hvac, "load_rules", lambda: {})
    monkeypatch.setattr(hvac, "load_schedules", lambda: {UID: [sched]})
    monkeypatch.setattr(hvac, "fetch_devices", fake_fetch)
    monkeypatch.setattr(hvac, "set_device_state", fake_set_device_state)
    monkeypatch.setattr(hvac, "_coordinate_boost", no_boost)
    monkeypatch.setattr(hvac.asyncio, "sleep", blocked_sleep)
    monkeypatch.setattr(hvac.read_snapshot, "invalidate", lambda _k: None)

    async def scenario() -> None:
        state = hvac._EngineState()
        await asyncio.wait_for(hvac._tick(hvac.AutomationConfig(), state), timeout=2)
        task = state.verify_tasks[UID]
        assert not task.done()  # the tick did not wait for it
        assert writes == [{"power": True, "operation_mode": "Heat", "set_temperature": 23.0,
                           "fan_speed": None, "vane_vertical_direction": None,
                           "vane_horizontal_direction": None}]
        task.cancel()

    asyncio.run(scenario())


def test_verification_off_spawns_nothing(monkeypatch) -> None:
    state = hvac._EngineState()
    config = hvac.AutomationConfig(verify=VerifyConfig(enabled=False))
    hvac._start_verification(config, state, _unit(), SimpleNamespace(time="08:00", power=False))
    assert state.verify_tasks == {}
