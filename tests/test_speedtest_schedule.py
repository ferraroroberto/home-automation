"""Opt-in nightly speed test (#840): when it is due, and that it never over-runs.

The speed test itself is never executed — ``fetch_internet_health`` is replaced
by a counting fake, and ``_run_speedtest`` (the one place real bandwidth is
spent) is booby-trapped so a stray real call fails the test loudly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

import src.network_history as nh
import src.speedtest_prefs as prefs_mod
from app.webapp import speedtest_schedule as sched
from src.network_client import InternetHealth

class _Calls(list):
    """Call counter (its length) that also carries the fake result to return."""

    result: dict


NIGHT = datetime(2026, 10, 9, 3, 10)  # inside the window, local time


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nh, "DEFAULT_DB_PATH", tmp_path / "network_history.sqlite3")
    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "speedtest_prefs.json")

    def _no_real_speedtest(*_a, **_k):
        raise AssertionError("a real speed test was attempted")

    monkeypatch.setattr("src.network_host._run_speedtest", _no_real_speedtest)


@pytest.fixture
def fake_test(monkeypatch: pytest.MonkeyPatch) -> "_Calls":
    """Replace the speed-test boundary; the list's length is the call count."""
    calls = _Calls()
    result = {"down": 300.0, "up": 40.0}

    async def fake(include_speedtest: bool = False, gateway=None) -> InternetHealth:
        assert include_speedtest is True
        calls.append(1)
        return InternetHealth(
            online=True, external_ms=12.0, download_mbps=result["down"], upload_mbps=result["up"]
        )

    monkeypatch.setattr(sched, "fetch_internet_health", fake)
    calls.result = result
    return calls


def _enable() -> None:
    prefs_mod.save_speedtest_prefs(prefs_mod.SpeedtestPrefs(nightly_enabled=True))


def _tick(state: sched._NightState, now: datetime = NIGHT) -> None:
    asyncio.run(sched._tick(state, now))


def test_default_is_off() -> None:
    assert prefs_mod.load_speedtest_prefs().nightly_enabled is False


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 10, 9, 2, 59), False),
        (datetime(2026, 10, 9, 3, 0), True),
        (datetime(2026, 10, 9, 5, 59), True),
        (datetime(2026, 10, 9, 6, 0), False),
        (datetime(2026, 10, 9, 14, 0), False),
    ],
)
def test_window(now: datetime, expected: bool) -> None:
    assert sched.is_due(now, None) is expected


def test_not_due_when_tonights_result_exists() -> None:
    assert sched.is_due(NIGHT, _ts(datetime(2026, 10, 9, 3, 1))) is False
    # Last night's result does not count for tonight.
    assert sched.is_due(NIGHT, _ts(datetime(2026, 10, 8, 3, 1))) is True


def test_off_runs_nothing_even_when_due(fake_test: "_Calls") -> None:
    _tick(sched._NightState())
    assert fake_test == []


def test_on_and_due_runs_once_and_stores_the_result(fake_test: "_Calls") -> None:
    _enable()
    state = sched._NightState()
    _tick(state)
    assert len(fake_test) == 1
    assert nh.last_speed_sample_ts() == _ts(NIGHT)
    history = nh.internet_history(now=_ts(NIGHT) + 60)
    assert history["download"] == [[_ts(NIGHT), 300.0]]
    assert history["upload"] == [[_ts(NIGHT), 40.0]]
    # Stored, so a second tick (or a restart with fresh state) does not repeat it.
    later = datetime(2026, 10, 9, 3, 40)
    _tick(state, later)
    _tick(sched._NightState(), later)
    assert len(fake_test) == 1


def test_on_but_outside_the_window_runs_nothing(fake_test: "_Calls") -> None:
    _enable()
    _tick(sched._NightState(), datetime(2026, 10, 9, 15, 0))
    assert fake_test == []


def test_a_failing_test_is_retried_a_bounded_number_of_times(
    fake_test: "_Calls", monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable()
    fake_test.result["down"] = None
    fake_test.result["up"] = None
    state = sched._NightState()
    for _ in range(10):
        _tick(state)
        # Pretend the retry gap has elapsed each time.
        state.last_attempt -= sched.RETRY_GAP_S + 1
    assert len(fake_test) == sched.MAX_ATTEMPTS
    # A new night starts a fresh budget.
    _tick(state, datetime(2026, 10, 10, 3, 5))
    assert len(fake_test) == sched.MAX_ATTEMPTS + 1


def test_retries_wait_for_the_gap(fake_test: "_Calls") -> None:
    _enable()
    fake_test.result["down"] = None
    fake_test.result["up"] = None
    state = sched._NightState()
    _tick(state)
    _tick(state)  # immediately after: inside the gap
    assert len(fake_test) == 1


def test_an_exception_in_the_test_never_escapes(
    fake_test: "_Calls", monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable()

    async def boom(include_speedtest: bool = False, gateway=None):
        raise RuntimeError("link down")

    monkeypatch.setattr(sched, "fetch_internet_health", boom)
    _tick(sched._NightState())  # must not raise


def test_process_gate_disables_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEEDTEST_SCHEDULE_ENABLED", "0")
    assert sched.start_nightly_speedtest() is None
