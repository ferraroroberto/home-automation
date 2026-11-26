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
from app.webapp._task_loop import BACKGROUND_TASKS, spawn_background_task
from src.devices import blind_automation as B
from src.devices.tuya_client import TuyaCommandError, TuyaDeviceInfo

MONDAY_0800 = datetime(2026, 10, 5, 8, 0, 30)
# The backoff is faked, so a drain takes milliseconds: seconds mean a stuck task.
DRAIN_TIMEOUT_S = 10.0


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


# ------------------------------------------------------------------- drain
async def _drain(timeout: float = DRAIN_TIMEOUT_S) -> None:
    """Await the background tasks; fail, naming them, if any outlives ``timeout``.

    ``asyncio.wait`` always yields to the loop. ``gather`` over tasks that have
    all finished does not, so a task whose ``discard`` callback has not run yet
    would keep ``while BACKGROUND_TASKS`` spinning forever.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while BACKGROUND_TASKS:
        done, _ = await asyncio.wait(
            set(BACKGROUND_TASKS), timeout=max(0.0, deadline - loop.time())
        )
        for task in done:
            task.result()  # a task that raised fails the test
        if BACKGROUND_TASKS and loop.time() >= deadline:
            names = ", ".join(sorted(
                f"{t.get_name()} ({t.get_coro().__qualname__})" for t in BACKGROUND_TASKS
            ))
            raise AssertionError(f"background task(s) still pending after {timeout:g}s: {names}")


def test_drain_returns_when_a_finished_task_is_not_yet_discarded() -> None:
    async def _done_at_once() -> None:
        return None

    async def _go() -> None:
        task = spawn_background_task(_done_at_once(), name="done-retry")
        await asyncio.sleep(0)
        # The window that hung the suite: finished, discard callback still queued.
        assert task.done() and task in BACKGROUND_TASKS
        await _drain()
        assert task not in BACKGROUND_TASKS

    asyncio.run(_go())


def test_drain_fails_naming_a_task_that_never_finishes() -> None:
    async def _never_done() -> None:
        while True:
            await asyncio.sleep(0)  # all the fake clock's sleep does

    async def _go() -> None:
        spawn_background_task(_never_done(), name="stuck-retry")
        with pytest.raises(AssertionError, match=r"still pending after 0\.2s: stuck-retry"):
            await _drain(timeout=0.2)

    asyncio.run(_go())


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
        await _drain()
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
        assert clock.slept == [] and BACKGROUND_TASKS
        assert state.latch.days["up"] == "2026-10-05"  # settled for today already

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
    assert "up" not in state.latch.days


def test_follow_alarm_returns_at_once_then_retries_and_notifies(house, lan, clock) -> None:
    lan.down = {"blind-1": 99}

    def _returned_before_any_retry() -> None:
        assert clock.slept == [] and BACKGROUND_TASKS

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


# ---------------------------------------------- Stop / reverse mid-move (#1264)
class HeldLan(FakeLan):
    """Blinds whose "open" hangs until released: a slow blind mid-move.

    ``received`` is what the blinds actually acted on, in the order they did:
    a send counts once its call returns, as on the real LAN. ``delivered_while_held``
    snapshots ``received`` the moment the test releases the held opens.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.received: List[tuple] = []

    def set_cover(self, device_id: str, action: str) -> dict:
        if action == "open":
            self.entered.set()
            assert self.release.wait(5), "test never released the held open"
        with self._lock:
            self.received.append((device_id, action))
        return {}


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch, lan: FakeLan) -> HeldLan:
    fake = HeldLan()
    monkeypatch.setattr(B, "set_cover", fake.set_cover)
    return fake


def _tap_while_open_is_held(
    held: HeldLan, *later: str, targets: Optional[List[str]] = None
) -> tuple[List[List[B.BlindOutcome]], List[tuple]]:
    """Tap "open" on ``targets`` (default blind-2), then each of ``later`` while it hangs.

    Returns every tap's outcomes and what the blinds received before the
    held opens were released.
    """
    ids = targets or ["blind-2"]

    async def _go() -> tuple[List[List[B.BlindOutcome]], List[tuple]]:
        first = asyncio.create_task(B.move_blinds("open", ids))
        assert await asyncio.to_thread(held.entered.wait, 5)
        taps = []
        for action in later:
            taps.append(asyncio.create_task(B.move_blinds(action, ids)))  # type: ignore[arg-type]
            await asyncio.sleep(0.1)  # taps reach the blinds in tap order
        await asyncio.sleep(0.2)
        with held._lock:
            while_held = list(held.received)
        held.release.set()
        return list(await asyncio.gather(first, *taps)), while_held

    return asyncio.run(_go())


def test_stop_mid_move_reaches_the_blind_at_once_and_stays_last(held) -> None:
    """Stop is not queued behind the slow open, and is re-sent after it returns."""
    (_up, stop), while_held = _tap_while_open_is_held(held, "stop")
    assert while_held == [("blind-2", "stop")]
    assert held.received[-1] == ("blind-2", "stop")
    assert stop[0].ok and not stop[0].superseded


def test_reverse_mid_move_reaches_the_blind_at_once_and_stays_last(held) -> None:
    (_up, down), while_held = _tap_while_open_is_held(held, "close")
    assert while_held == [("blind-2", "close")]
    assert held.received[-1] == ("blind-2", "close")
    assert down[0].ok


def test_all_blinds_stop_mid_move_reaches_every_blind_at_once(held) -> None:
    """The group buttons: Stop reaches every blind while each one's open hangs."""
    (_up, stop), while_held = _tap_while_open_is_held(
        held, "stop", targets=["blind-1", "blind-2"]
    )
    assert sorted(while_held) == [("blind-1", "stop"), ("blind-2", "stop")]
    for blind in ("blind-1", "blind-2"):
        mine = [a for d, a in held.received if d == blind]
        assert mine[-1] == "stop"
    assert all(o.ok for o in stop)


def test_down_then_stop_mid_move_ends_on_stop(held) -> None:
    """Down then Stop tapped while Up hangs: both go out at once, Stop stays last."""
    (_up, down, stop), while_held = _tap_while_open_is_held(held, "close", "stop")
    assert while_held == [("blind-2", "close"), ("blind-2", "stop")]
    assert held.received[-1] == ("blind-2", "stop")
    assert down[0].ok and stop[0].ok


def test_a_stale_send_still_waiting_its_turn_is_dropped(lan) -> None:
    """A command a newer one superseded before it was sent never goes out."""
    stale = B.note_blind_command(["blind-2"])
    B.note_blind_command(["blind-2"])
    outcome = B._send_newest("blind-2", "open", stale)
    assert outcome.superseded and not outcome.ok
    assert lan.sent == []


def test_stop_cancels_the_pending_retry_of_a_failed_move(house, lan, clock) -> None:
    """A Stop is never undone: the failed "open" it overtook is not retried."""
    lan.down = {"blind-2": 1}

    async def _go() -> None:
        outcomes = await B.move_blinds("open")
        await engine.settle_manual_move("open", outcomes)
        await B.move_blinds("stop")
        await _drain()

    asyncio.run(_go())
    assert [a for d, a in lan.sent if d == "blind-2"] == ["open", "stop"]
    assert house.messages == []


def test_manual_move_retries_on_the_backoff_then_notifies_once(house, lan, clock) -> None:
    lan.down = {"blind-2": 99}

    async def _go() -> None:
        outcomes = await B.move_blinds("close")
        await engine.settle_manual_move("close", outcomes)
        assert clock.slept == [] and BACKGROUND_TASKS  # the tap answered at once
        await _drain()

    asyncio.run(_go())
    assert clock.slept == [30.0, 60.0, 120.0, 240.0]
    assert lan.tries("blind-2") == 5 and lan.tries("blind-1") == 1
    assert len(house.messages) == 1
    assert "could not close" in house.messages[0] and "Renamed Two" in house.messages[0]
    assert "(manual)" in house.messages[0] and "~7.5 min" in house.messages[0]


def test_manual_move_that_recovers_is_log_only(house, lan, clock) -> None:
    lan.down = {"blind-1": 1}
    _run_then_drain(lambda: _manual("open"))
    assert clock.slept == [30.0] and lan.tries("blind-1") == 2
    assert house.messages == [] and house.events == []  # no event record for a tap


def test_a_failed_stop_retries_within_the_travel_time(house, lan, clock) -> None:
    lan.down = {"blind-1": 99}
    _run_then_drain(lambda: _manual("stop"))
    assert clock.slept == list(B.STOP_RETRY_DELAYS_S)
    assert len(house.messages) == 1 and "could not stop" in house.messages[0]
    assert "~14 s" in house.messages[0]


async def _manual(action: str) -> None:
    await engine.settle_manual_move(action, await B.move_blinds(action))  # type: ignore[arg-type]
