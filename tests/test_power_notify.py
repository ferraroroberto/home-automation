"""Unit tests for the UPS power-event notification path.

Covers :mod:`src.power_notify_prefs`, :func:`app.webapp.power_notify.record_power_event`,
and the edge-triggering in :func:`app.webapp.power_monitor.tick` (baseline on
first read, fire only on a mains↔battery transition). No subprocess, no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional

import app.webapp.power_monitor as PM
from app.webapp import power_notify
from app.webapp.power_notify import FleetShutdownResult
from src import activity_log
from src.notify import NotifierError
from src.pc_fleet_prefs import PcFleetPrefs
from src.power_notify_prefs import (
    PowerNotifyPrefs,
    load_power_notify_prefs,
    save_power_notify_prefs,
)
from src.ups_client import UpsState


def _fleet_noop(hub_reachable: bool = True):
    """A fleet_shutdown_fn stub that reaches the hub but shuts down nobody."""

    async def _fn(**_kw) -> FleetShutdownResult:
        return FleetShutdownResult(hub_reachable=hub_reachable, outcomes=[])

    return _fn


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


class BoomNotifier:
    def __init__(self) -> None:
        self.attempts = 0

    def send_text(self, text: str) -> None:
        self.attempts += 1
        raise NotifierError("boom")


class FlakyNotifier:
    """Fails on the first attempt, then succeeds — a transient send failure."""

    def __init__(self) -> None:
        self.sent: List[str] = []
        self.attempts = 0

    def send_text(self, text: str) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise NotifierError("transient")
        self.sent.append(text)


def _read_power_log(tmp_path: Path) -> List[dict]:
    path = tmp_path / "power.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------- prefs store


def test_power_prefs_default_both_on(tmp_path: Path) -> None:
    prefs = load_power_notify_prefs(tmp_path / "absent.json")
    assert prefs == PowerNotifyPrefs(power_lost=True, power_restored=True)


def test_power_prefs_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    save_power_notify_prefs(
        PowerNotifyPrefs(power_lost=True, power_restored=False),
        path,
    )
    loaded = load_power_notify_prefs(path)
    assert loaded.power_lost is True and loaded.power_restored is False
    assert not (tmp_path / "p.json.tmp").exists()


def test_power_prefs_dropped_auto_shutdown_field() -> None:
    # The former auto_shutdown_low_battery toggle moved to src.pc_fleet_prefs.
    assert not hasattr(PowerNotifyPrefs(), "auto_shutdown_low_battery")


# ---------------------------------------------------- record_power_event


def test_power_lost_notifies_and_logs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    asyncio.run(power_notify.record_power_event(
        lost=True, detail="59min runtime",
        prefs_loader=lambda: PowerNotifyPrefs(power_lost=True),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1 and "LOST" in notifier.sent[0]
    assert _read_power_log(tmp_path)[0]["event"] == "power_lost"


def test_power_restored_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    # power_restored off → logged, not sent.
    asyncio.run(power_notify.record_power_event(
        lost=False,
        prefs_loader=lambda: PowerNotifyPrefs(power_restored=False),
        notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []
    assert _read_power_log(tmp_path)[0]["event"] == "power_restored"


def test_power_delivery_failure_swallowed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    # Skip the real retry delay — this test cares about exhaustion behavior,
    # not timing.
    monkeypatch.setattr(power_notify, "_SEND_RETRY_DELAYS_S", ())
    boom = BoomNotifier()
    asyncio.run(power_notify.record_power_event(
        lost=True,
        prefs_loader=lambda: PowerNotifyPrefs(power_lost=True),
        notifier_factory=lambda: boom,
    ))
    assert _read_power_log(tmp_path)[0]["mains_online"] is False
    assert boom.attempts == 1


def test_power_restored_delivered_after_transient_failure(tmp_path: Path, monkeypatch) -> None:
    """Issue #394: a single transient send failure must not permanently lose
    the edge-triggered "restored" alert — one retry should still deliver it."""
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(power_notify.time, "sleep", lambda _seconds: None)
    flaky = FlakyNotifier()
    asyncio.run(power_notify.record_power_event(
        lost=False,
        prefs_loader=lambda: PowerNotifyPrefs(power_restored=True),
        notifier_factory=lambda: flaky,
    ))
    assert flaky.attempts == 2
    assert len(flaky.sent) == 1 and "restored" in flaky.sent[0]


# --------------------------------------------- record_low_battery_shutdown


def test_low_battery_shutdown_enabled_notifies_and_shuts_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    calls: List[dict] = []

    def fake_shutdown(**kwargs) -> bool:
        calls.append(kwargs)
        return True

    result = asyncio.run(power_notify.record_low_battery_shutdown(
        detail="12min runtime",
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: notifier,
        shutdown_fn=fake_shutdown,
        fleet_shutdown_fn=_fleet_noop(),
    ))
    assert result is True
    assert len(notifier.sent) == 1 and "Tower shutting down last" in notifier.sent[0]
    assert calls == [{"grace_seconds": 180, "message": "Low UPS battery — PC shutting down to avoid data loss"}]
    log = _read_power_log(tmp_path)
    assert log[0]["event"] == "low_battery_shutdown"
    assert log[0]["detail"] == "12min runtime"
    assert log[0]["fleet_enabled"] is True


def test_low_battery_shutdown_disabled_logs_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    calls: List[dict] = []
    fleet_calls: List[dict] = []

    async def spy_fleet(**kw) -> FleetShutdownResult:
        fleet_calls.append(kw)
        return FleetShutdownResult(hub_reachable=True, outcomes=[])

    result = asyncio.run(power_notify.record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=False),
        notifier_factory=lambda: notifier,
        shutdown_fn=lambda **kw: calls.append(kw) or True,
        fleet_shutdown_fn=spy_fleet,
    ))
    assert result is False
    assert notifier.sent == []
    assert calls == []          # tower not shut down
    assert fleet_calls == []    # satellites never touched
    log = _read_power_log(tmp_path)
    assert log[0]["event"] == "low_battery_shutdown"
    assert log[0]["fleet_enabled"] is False


def test_low_battery_shutdown_delivery_failure_still_shuts_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(power_notify, "_SEND_RETRY_DELAYS_S", ())
    calls: List[dict] = []
    result = asyncio.run(power_notify.record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: BoomNotifier(),
        shutdown_fn=lambda **kw: calls.append(kw) or True,
        fleet_shutdown_fn=_fleet_noop(),
    ))
    assert result is True
    assert len(calls) == 1


def test_low_battery_shutdown_cancelled_logs_and_cancels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    calls: List[bool] = []
    power_notify.record_low_battery_shutdown_cancelled(cancel_fn=lambda: calls.append(True) or True)
    assert calls == [True]
    assert _read_power_log(tmp_path)[0]["event"] == "low_battery_shutdown_cancelled"


# ----------------------------------------------------- power_monitor.tick


def _ups(mains_online: bool, runtime_seconds: int = 3600) -> UpsState:
    return UpsState(
        available=True, source="test", mains_online=mains_online, runtime_seconds=runtime_seconds
    )


def test_monitor_baseline_then_transitions(monkeypatch) -> None:
    events: List[bool] = []

    async def fake_record_power_event(**kw) -> None:
        events.append(kw["lost"])

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)

    holder = {"ups": _ups(True)}
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: holder["ups"])

    state = PM._MonitorState()
    asyncio.run(PM.tick(state))            # first read → baseline, no event
    assert events == []
    assert state.last_mains_online is True

    holder["ups"] = _ups(False)
    asyncio.run(PM.tick(state))            # mains lost
    holder["ups"] = _ups(False)
    asyncio.run(PM.tick(state))            # still on battery → no repeat
    holder["ups"] = _ups(True)
    asyncio.run(PM.tick(state))            # restored

    assert events == [True, False]         # one lost, one restored, no spam


def test_monitor_ignores_unavailable_ups(monkeypatch) -> None:
    events: List[bool] = []

    async def fake_record_power_event(**kw) -> None:
        events.append(kw["lost"])

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: UpsState(available=False, source="none"))
    state = PM._MonitorState()
    asyncio.run(PM.tick(state))
    assert events == [] and state.last_mains_online is None


# --------------------------------------- power_monitor.tick — low-battery shutdown


def test_monitor_triggers_shutdown_once_below_threshold(monkeypatch) -> None:
    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []

    async def fake_record_low_battery_shutdown(**kw) -> None:
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)
    cancel_calls: List[bool] = []
    monkeypatch.setattr(PM, "record_low_battery_shutdown_cancelled", lambda: cancel_calls.append(True))

    holder = {"ups": _ups(True)}
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: holder["ups"])
    loader = lambda: PcFleetPrefs(threshold_minutes=15)  # noqa: E731 — hermetic threshold
    state = PM._MonitorState()
    asyncio.run(PM.tick(state, prefs_loader=loader))  # baseline, online

    holder["ups"] = _ups(False, runtime_seconds=1200)  # mains lost, 20min left — above threshold
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == []
    assert state.low_battery_shutdown_triggered is False

    holder["ups"] = _ups(False, runtime_seconds=900)  # crosses the 15min threshold
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == ["15min runtime"]
    assert state.low_battery_shutdown_triggered is True

    holder["ups"] = _ups(False, runtime_seconds=600)  # still low → no repeat trigger
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == ["15min runtime"]

    holder["ups"] = _ups(True)  # mains restored → cancel the pending shutdown
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert cancel_calls == [True]
    assert state.low_battery_shutdown_triggered is False


def test_monitor_threshold_read_from_prefs(monkeypatch) -> None:
    """The trigger point is PcFleetPrefs.threshold_minutes, not the constant:
    a 30-min pref must fire at 30 min of remaining runtime, not 15."""

    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []

    async def fake_record_low_battery_shutdown(**kw) -> None:
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)

    holder = {"ups": _ups(True)}
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: holder["ups"])
    loader = lambda: PcFleetPrefs(threshold_minutes=30)  # noqa: E731
    state = PM._MonitorState()
    asyncio.run(PM.tick(state, prefs_loader=loader))  # baseline

    holder["ups"] = _ups(False, runtime_seconds=1500)  # 25min — under default 15 would NOT fire, but 25<30 does
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == ["25min runtime"]
    assert state.low_battery_shutdown_triggered is True


def test_monitor_threshold_falls_back_when_prefs_raise(monkeypatch) -> None:
    """A prefs-load failure must not disable the safety net — fall back to the
    15-min constant."""

    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []

    async def fake_record_low_battery_shutdown(**kw) -> None:
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)

    def boom() -> PcFleetPrefs:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(PM, "fetch_ups_state", lambda: _ups(False, runtime_seconds=800))
    state = PM._MonitorState()
    asyncio.run(PM.tick(state, prefs_loader=boom))  # 800s < 900s constant → fires
    assert shutdown_calls == ["13min runtime"]


def test_monitor_triggers_shutdown_on_first_observation_when_already_critical(monkeypatch) -> None:
    """A safety measure, unlike the mains-transition alert, fires even at startup."""

    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []

    async def fake_record_low_battery_shutdown(**kw) -> None:
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: _ups(False, runtime_seconds=300))

    state = PM._MonitorState()
    asyncio.run(PM.tick(state, prefs_loader=lambda: PcFleetPrefs(threshold_minutes=15)))

    assert shutdown_calls == ["5min runtime"]
    assert state.low_battery_shutdown_triggered is True


def test_monitor_unknown_runtime_never_triggers_shutdown(monkeypatch) -> None:
    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []

    async def fake_record_low_battery_shutdown(**kw) -> None:
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: _ups(False, runtime_seconds=None))

    state = PM._MonitorState()
    asyncio.run(PM.tick(state))

    assert shutdown_calls == []
