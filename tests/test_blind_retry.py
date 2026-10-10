"""Retrying a blind that fails an automatic move (issue #897).

No real blind is ever touched and no real Telegram message is sent: the Tuya
LAN is a fake ``set_cover``/``list_devices``, the backoff runs on a fake clock
that only records the delays, and the notifier is a recorder.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

import pytest

from app.webapp import blind_schedules as engine
from src import blind_automation as B
from src.tuya_client import TuyaCommandError, TuyaDeviceInfo

MONDAY_0800 = datetime(2026, 10, 5, 8, 0, 30)


class FakeLan:
    """Blinds that each fail their first ``down[id]`` commands, then answer."""

    def __init__(self) -> None:
        self.down: Dict[str, int] = {}
        self.sent: List[tuple] = []
        self._lock = threading.Lock()

    def set_cover(self, device_id: str, action: str) -> dict:
        with self._lock:
            self.sent.append((device_id, action))
            if self.down.get(device_id, 0) > 0:
                self.down[device_id] -= 1
                raise TuyaCommandError(f"{device_id} did not answer")
        return {}

    def tries(self, device_id: str) -> int:
        return sum(1 for d, _ in self.sent if d == device_id)


class FakeClock:
    """Records each backoff delay; ``on_sleep`` runs at every wake-up."""

    def __init__(self) -> None:
        self.slept: List[float] = []
        self.on_sleep: Optional[Callable[[float], None]] = None

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        if self.on_sleep is not None:
            self.on_sleep(delay)
        await asyncio.sleep(0)


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> FakeLan:
    fake = FakeLan()
    infos = [
        TuyaDeviceInfo(device_id="blind-1", name="Blind One", cover_control_dps="1"),
        TuyaDeviceInfo(device_id="blind-2", name="Blind Two", cover_control_dps="1"),
    ]
    monkeypatch.setattr(B, "list_devices", lambda: infos)
    monkeypatch.setattr(B, "set_cover", fake.set_cover)
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# -------------------------------------------------------------------- core
def _move_and_retry(lan: FakeLan, clock: FakeClock, action: str = "open") -> B.BlindRetryReport:
    async def _go() -> B.BlindRetryReport:
        outcomes = await B.move_blinds(action)  # type: ignore[arg-type]
        return await B.retry_failed_blinds(action, outcomes, sleep=clock.sleep)  # type: ignore[arg-type]

    return asyncio.run(_go())


def test_full_success_never_sleeps_or_resends(lan, clock) -> None:
    report = _move_and_retry(lan, clock)
    assert clock.slept == []
    assert report.failed == [] and sorted(report.moved) == ["blind-1", "blind-2"]
    assert report.max_attempts == 1


def test_failed_blind_succeeds_on_a_later_retry(lan, clock) -> None:
    lan.down = {"blind-2": 2}  # fails the first try and the 30 s retry
    report = _move_and_retry(lan, clock)
    assert clock.slept == [30.0, 60.0]
    assert lan.tries("blind-1") == 1  # a blind that moved is never re-sent
    assert lan.tries("blind-2") == 3
    assert report.failed == [] and report.attempts == {"blind-1": 1, "blind-2": 3}


def test_final_failure_after_the_whole_backoff(lan, clock) -> None:
    lan.down = {"blind-2": 99}
    report = _move_and_retry(lan, clock)
    assert clock.slept == list(B.RETRY_DELAYS_S) == [30.0, 60.0, 120.0, 240.0]
    assert lan.tries("blind-2") == 5
    assert report.failed == ["blind-2"] and report.max_attempts == 5


def test_a_newer_command_cancels_the_pending_retry(lan, clock) -> None:
    lan.down = {"blind-2": 1}
    # While the "open" retry waits, the owner taps "close" on that blind.
    clock.on_sleep = lambda _delay: B.note_blind_command(["blind-2"])
    report = _move_and_retry(lan, clock, "open")
    assert ("blind-2", "open") in lan.sent and lan.sent.count(("blind-2", "open")) == 1
    assert report.superseded == ["blind-2"] and report.failed == []


# ------------------------------------------------------------------ engine
@pytest.fixture
def house(monkeypatch: pytest.MonkeyPatch, lan: FakeLan, clock: FakeClock, tmp_path):
    """The engine wired to the fake LAN, fake clock and a recording notifier."""
    state = SimpleNamespace(messages=[], records=[], events=[])
    monkeypatch.setattr(B, "SCHEDULES_PATH", tmp_path / "blind_schedules.json")
    monkeypatch.setattr(engine, "_retry_sleep", clock.sleep)
    monkeypatch.setattr(engine, "load_people", lambda: {"p1": SimpleNamespace(state="home")})
    monkeypatch.setattr(engine, "list_devices", B.list_devices)
    monkeypatch.setattr(engine, "load_tuya_display_names", lambda: {"blind-2": "Renamed Two"})
    monkeypatch.setattr(
        engine, "build_alarm_notifier",
        lambda: SimpleNamespace(send_text=state.messages.append),
    )
    monkeypatch.setattr(engine, "_record", lambda entry, *a: state.records.append(a))
    monkeypatch.setattr(engine, "_record_event", lambda *a: state.events.append(a))
    monkeypatch.setattr(engine, "load_blind_alarm_prefs",
                        lambda: B.BlindAlarmPrefs(follow_alarm=True))
    return state


def _run_then_drain(coro_factory, check_before_drain=None):
    """Run the engine call, optionally inspect, then let its retries finish."""
    async def _go():
        result = await coro_factory()
        if check_before_drain is not None:
            check_before_drain()
        while engine._RETRY_TASKS:
            await asyncio.gather(*list(engine._RETRY_TASKS))
        return result

    return asyncio.run(_go())


def _tick_schedule(state: engine._EngineState, check=None) -> None:
    _run_then_drain(
        lambda: engine.tick(engine.BlindScheduleConfig(), state, MONDAY_0800), check
    )


def test_schedule_success_records_one_attempt_and_sends_nothing(house, lan, clock) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}])
    _tick_schedule(engine._EngineState())
    assert house.messages == []
    outcome, detail, extra = house.records[0]
    assert outcome == "ok" and extra["attempts"] == 1 and extra["failed"] == []


def test_schedule_retries_in_the_background_without_holding_the_tick(house, lan, clock) -> None:
    lan.down = {"blind-2": 2}
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}])
    state = engine._EngineState()

    def _tick_returned_before_any_retry() -> None:
        assert clock.slept == [] and engine._RETRY_TASKS
        assert state.last_fire_day["up"] == "2026-10-05"  # settled for today already

    _tick_schedule(state, _tick_returned_before_any_retry)
    assert clock.slept == [30.0, 60.0]
    assert house.messages == []  # success after retries: log only
    outcome, detail, extra = house.records[0]
    assert outcome == "ok" and extra["attempts"] == 3
    assert detail == "2 moved, 0 failed after 3 attempt(s)"


def test_schedule_final_failure_sends_exactly_one_message(house, lan, clock) -> None:
    lan.down = {"blind-1": 99, "blind-2": 99}
    B.set_blind_schedules([{"id": "down", "time": "08:00", "action": "close"}])
    state = engine._EngineState()
    _tick_schedule(state)
    _tick_schedule(state)  # the next poll in the window does not fire it again
    assert len(house.messages) == 1
    message = house.messages[0]
    assert "could not close" in message
    assert "Blind One" in message and "Renamed Two" in message  # display names
    assert "schedule 08:00" in message and "~7.5 min" in message
    outcome, _detail, extra = house.records[0]
    assert outcome == "error" and extra["attempts"] == 5
    assert extra["failed"] == ["blind-1", "blind-2"]


def test_an_unattempted_move_stays_due_for_the_next_poll(house, monkeypatch) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}])

    async def _boom(*_a, **_k):
        raise RuntimeError("devices.json unreadable")

    monkeypatch.setattr(engine, "move_blinds", _boom)
    state = engine._EngineState()
    _tick_schedule(state)
    assert "up" not in state.last_fire_day


def test_follow_alarm_returns_at_once_then_retries_and_notifies(house, lan, clock) -> None:
    lan.down = {"blind-1": 99}

    def _returned_before_any_retry() -> None:
        assert clock.slept == [] and engine._RETRY_TASKS

    result = _run_then_drain(
        lambda: engine.follow_alarm("arm", "arm"), _returned_before_any_retry
    )
    assert result == "close"
    assert len(house.messages) == 1 and "following the alarm" in house.messages[0]
    action, entity, source, outcome, payload = house.events[0]
    assert (action, source, outcome) == ("close", "alarm", "error")
    assert payload["attempts"] == 5 and payload["failed"] == ["blind-1"]
    assert payload["moved"] == 1 and payload["total"] == 2


def test_a_newer_schedule_supersedes_an_older_pending_retry(house, lan, clock) -> None:
    lan.down = {"blind-2": 99}
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}])
    # A later command reaches the blind while the "open" retry is pending.
    clock.on_sleep = lambda _delay: B.note_blind_command(["blind-2"])
    _tick_schedule(engine._EngineState())
    assert lan.tries("blind-2") == 1
    assert house.messages == []  # superseded is not a failure
    outcome, _detail, extra = house.records[0]
    assert outcome == "ok" and extra["superseded"] == ["blind-2"]
