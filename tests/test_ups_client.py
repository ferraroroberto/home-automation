"""Unit tests for the NUT backoff in :mod:`src.ups_client` (issue #537).

A NUT server that isn't running had its upsc.exe/usbhid-ups.exe subprocess
re-spawned (and timed out) on every poll tick; these tests cover the backoff
that stops that churn while still falling through to the Windows-battery
fallback. No real subprocess is ever spawned here.
"""

from __future__ import annotations

import pytest

from src import ups_client as U


@pytest.fixture(autouse=True)
def _clear_backoff_state() -> None:
    """Isolate the module-level NUT backoff trackers between tests."""
    U._nut_backoff.consecutive_failures = 0
    U._nut_backoff.next_retry_at = 0.0
    U._nut_direct_backoff.consecutive_failures = 0
    U._nut_direct_backoff.next_retry_at = 0.0
    yield
    U._nut_backoff.consecutive_failures = 0
    U._nut_backoff.next_retry_at = 0.0
    U._nut_direct_backoff.consecutive_failures = 0
    U._nut_direct_backoff.next_retry_at = 0.0


def test_source_backoff_escalates_and_caps() -> None:
    backoff = U.BackoffTracker()
    delays = [backoff.record_failure() for _ in range(8)]

    assert delays[0] == pytest.approx(U._BACKOFF_BASE_S)
    assert all(delays[i] <= delays[i + 1] + 0.01 for i in range(len(delays) - 1))
    assert delays[-1] <= U._BACKOFF_MAX_S + 0.01


def test_source_backoff_success_clears_state() -> None:
    backoff = U.BackoffTracker()
    backoff.record_failure()
    assert backoff.seconds_remaining() is not None

    backoff.record_success()

    assert backoff.seconds_remaining() is None
    assert backoff.consecutive_failures == 0


def test_fetch_ups_state_falls_through_to_windows_battery_on_nut_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"nut": 0, "direct": 0}

    def _fail_nut():
        calls["nut"] += 1
        raise RuntimeError("upsc timed out")

    def _fail_direct():
        calls["direct"] += 1
        raise RuntimeError("usbhid-ups timed out")

    windows_state = U.UpsState(available=True, source="windows_battery")
    monkeypatch.setattr(U, "_read_nut", _fail_nut)
    monkeypatch.setattr(U, "_read_nut_direct", _fail_direct)
    monkeypatch.setattr(U, "_read_windows_battery", lambda: windows_state)

    result = U.fetch_ups_state()

    assert result is windows_state
    assert calls == {"nut": 1, "direct": 1}
    assert U._nut_backoff.seconds_remaining() is not None
    assert U._nut_direct_backoff.seconds_remaining() is not None


def test_fetch_ups_state_skips_backed_off_nut_without_calling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    U._nut_backoff.record_failure()  # simulate a prior failure still in its window

    def _should_not_be_called():
        raise AssertionError("_read_nut must not be called while backed off")

    monkeypatch.setattr(U, "_read_nut", _should_not_be_called)
    monkeypatch.setattr(U, "_read_nut_direct", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    windows_state = U.UpsState(available=True, source="windows_battery")
    monkeypatch.setattr(U, "_read_windows_battery", lambda: windows_state)

    result = U.fetch_ups_state()

    assert result is windows_state


def test_fetch_ups_state_success_clears_backoff_and_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    U._nut_backoff.record_failure()
    U._nut_backoff.next_retry_at = 0.0  # simulate the backoff window having elapsed
    nut_state = U.UpsState(available=True, source="nut")

    def _direct_should_not_be_called():
        raise AssertionError("_read_nut_direct must not be called when NUT succeeds")

    monkeypatch.setattr(U, "_read_nut", lambda: nut_state)
    monkeypatch.setattr(U, "_read_nut_direct", _direct_should_not_be_called)

    result = U.fetch_ups_state()

    assert result is nut_state
    assert U._nut_backoff.seconds_remaining() is None
