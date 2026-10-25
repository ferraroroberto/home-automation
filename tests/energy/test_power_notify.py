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


# -------------------------------------------- tower_action = hibernate (#962)


def _hibernate_prefs() -> PcFleetPrefs:
    return PcFleetPrefs(enabled=True, tower_action="hibernate")


def test_low_battery_hibernate_waits_grace_then_hibernates(tmp_path: Path, monkeypatch) -> None:
    """Dispatch: hibernate is chosen, shutdown is not, and the alert names it."""
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    hibernated: List[bool] = []
    shut: List[dict] = []

    async def scenario() -> bool:
        result = await power_notify.record_low_battery_shutdown(
            grace_seconds=0,
            pc_fleet_loader=_hibernate_prefs,
            notifier_factory=lambda: notifier,
            shutdown_fn=lambda **kw: shut.append(kw) or True,
            hibernate_fn=lambda: hibernated.append(True) or True,
            fleet_shutdown_fn=_fleet_noop(),
        )
        await power_notify._pending_hibernate  # the grace window elapses
        return result

    assert asyncio.run(scenario()) is True
    assert hibernated == [True] and shut == []
    assert "Tower hibernating last" in notifier.sent[0]
    events = [r["event"] for r in _read_power_log(tmp_path)]
    assert events == ["low_battery_shutdown", "low_battery_hibernate"]
    assert _read_power_log(tmp_path)[0]["tower_action"] == "hibernate"


def test_low_battery_hibernate_does_not_fire_before_grace_elapses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    hibernated: List[bool] = []

    async def scenario() -> None:
        await power_notify.record_low_battery_shutdown(
            grace_seconds=3600,
            pc_fleet_loader=_hibernate_prefs,
            notifier_factory=lambda: None,
            hibernate_fn=lambda: hibernated.append(True) or True,
            fleet_shutdown_fn=_fleet_noop(),
        )
        await asyncio.sleep(0)  # let the timer task start
        assert not power_notify._pending_hibernate.done()
        power_notify._cancel_pending_hibernate()

    asyncio.run(scenario())
    assert hibernated == []


def test_mains_return_cancels_pending_hibernate(tmp_path: Path, monkeypatch) -> None:
    """The cancel path: mains back inside the window -> nothing hibernates, and
    the activity log records the cancellation."""
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    hibernated: List[bool] = []
    cancels: List[bool] = []

    async def scenario() -> None:
        await power_notify.record_low_battery_shutdown(
            grace_seconds=0.05,
            pc_fleet_loader=_hibernate_prefs,
            notifier_factory=lambda: None,
            hibernate_fn=lambda: hibernated.append(True) or True,
            fleet_shutdown_fn=_fleet_noop(),
        )
        task = power_notify._pending_hibernate
        power_notify.record_low_battery_shutdown_cancelled(
            cancel_fn=lambda: cancels.append(True) or True
        )
        await asyncio.sleep(0.15)  # well past the grace window
        assert task.cancelled()

    asyncio.run(scenario())
    assert hibernated == [] and cancels == [True]
    log = _read_power_log(tmp_path)
    assert log[-1]["event"] == "low_battery_shutdown_cancelled"
    assert log[-1]["hibernate_cancelled"] is True


def test_failed_hibernate_falls_back_to_a_shutdown(tmp_path: Path, monkeypatch) -> None:
    """Hibernation disabled in Windows must not leave the tower to run flat."""
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    shut: List[dict] = []

    async def scenario() -> None:
        await power_notify.record_low_battery_shutdown(
            grace_seconds=0,
            pc_fleet_loader=_hibernate_prefs,
            notifier_factory=lambda: None,
            shutdown_fn=lambda **kw: shut.append(kw) or True,
            hibernate_fn=lambda: False,
            fleet_shutdown_fn=_fleet_noop(),
        )
        await power_notify._pending_hibernate

    asyncio.run(scenario())
    assert len(shut) == 1
    assert shut[0]["grace_seconds"] == power_notify._HIBERNATE_FALLBACK_GRACE_S
    assert _read_power_log(tmp_path)[-1]["event"] == "low_battery_hibernate_failed"


def test_default_tower_action_still_uses_the_os_shutdown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    shut: List[dict] = []
    hibernated: List[bool] = []
    asyncio.run(power_notify.record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: None,
        shutdown_fn=lambda **kw: shut.append(kw) or True,
        hibernate_fn=lambda: hibernated.append(True) or True,
        fleet_shutdown_fn=_fleet_noop(),
    ))
    assert len(shut) == 1 and hibernated == []
    assert power_notify._pending_hibernate is None or power_notify._pending_hibernate.done()


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


def test_monitor_retries_after_shutdown_call_raises(monkeypatch) -> None:
    """Regression for #990: an unreadable pc-fleet store must not permanently
    disable the safety shutdown for the rest of the outage.

    ``record_low_battery_shutdown`` loads pc-fleet prefs itself and can raise
    (``StoreUnreadableError``) independently of the threshold fallback tested
    above. The once-per-outage flag used to be set *before* that call, so a
    raise left it ``True`` forever with no shutdown ever having happened.
    Fails on pre-fix code: the second tick's call never fires.
    """

    async def fake_record_power_event(**kw) -> None:
        pass

    monkeypatch.setattr(PM, "record_power_event", fake_record_power_event)
    shutdown_calls: List[Optional[str]] = []
    should_raise = {"value": True}

    async def fake_record_low_battery_shutdown(**kw) -> None:
        if should_raise["value"]:
            raise RuntimeError("pc_fleet.json unreadable")
        shutdown_calls.append(kw.get("detail"))

    monkeypatch.setattr(PM, "record_low_battery_shutdown", fake_record_low_battery_shutdown)

    holder = {"ups": _ups(True)}
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: holder["ups"])
    loader = lambda: PcFleetPrefs(threshold_minutes=15)  # noqa: E731
    state = PM._MonitorState()
    asyncio.run(PM.tick(state, prefs_loader=loader))  # baseline, online

    holder["ups"] = _ups(False, runtime_seconds=900)  # crosses threshold, call raises
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == []
    assert state.low_battery_shutdown_triggered is False  # must retry, not stick True

    should_raise["value"] = False
    holder["ups"] = _ups(False, runtime_seconds=600)  # still on battery, retried tick
    asyncio.run(PM.tick(state, prefs_loader=loader))
    assert shutdown_calls == ["10min runtime"]
    assert state.low_battery_shutdown_triggered is True


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
