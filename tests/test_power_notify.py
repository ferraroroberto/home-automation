"""Unit tests for the UPS power-event notification path.

Covers :mod:`src.power_notify_prefs`, :func:`app.webapp.power_notify.record_power_event`,
and the edge-triggering in :func:`app.webapp.power_monitor.tick` (baseline on
first read, fire only on a mains↔battery transition). No subprocess, no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List

import app.webapp.power_monitor as PM
from app.webapp import power_notify
from src import activity_log
from src.notify import NotifierError
from src.power_notify_prefs import (
    PowerNotifyPrefs,
    load_power_notify_prefs,
    save_power_notify_prefs,
)
from src.ups_client import UpsState


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


class BoomNotifier:
    def send_text(self, text: str) -> None:
        raise NotifierError("boom")


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
    save_power_notify_prefs(PowerNotifyPrefs(power_lost=True, power_restored=False), path)
    loaded = load_power_notify_prefs(path)
    assert loaded.power_lost is True and loaded.power_restored is False
    assert not (tmp_path / "p.json.tmp").exists()


# ---------------------------------------------------- record_power_event


def test_power_lost_notifies_and_logs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    power_notify.record_power_event(
        lost=True, detail="59min runtime",
        prefs_loader=lambda: PowerNotifyPrefs(power_lost=True),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 1 and "LOST" in notifier.sent[0]
    assert _read_power_log(tmp_path)[0]["event"] == "power_lost"


def test_power_restored_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    # power_restored off → logged, not sent.
    power_notify.record_power_event(
        lost=False,
        prefs_loader=lambda: PowerNotifyPrefs(power_restored=False),
        notifier_factory=lambda: notifier,
    )
    assert notifier.sent == []
    assert _read_power_log(tmp_path)[0]["event"] == "power_restored"


def test_power_delivery_failure_swallowed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    power_notify.record_power_event(
        lost=True,
        prefs_loader=lambda: PowerNotifyPrefs(power_lost=True),
        notifier_factory=lambda: BoomNotifier(),
    )
    assert _read_power_log(tmp_path)[0]["mains_online"] is False


# ----------------------------------------------------- power_monitor.tick


def _ups(mains_online: bool) -> UpsState:
    return UpsState(available=True, source="test", mains_online=mains_online, runtime_seconds=3600)


def test_monitor_baseline_then_transitions(monkeypatch) -> None:
    events: List[bool] = []
    monkeypatch.setattr(PM, "record_power_event", lambda **kw: events.append(kw["lost"]))

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
    monkeypatch.setattr(PM, "record_power_event", lambda **kw: events.append(kw["lost"]))
    monkeypatch.setattr(PM, "fetch_ups_state", lambda: UpsState(available=False, source="none"))
    state = PM._MonitorState()
    asyncio.run(PM.tick(state))
    assert events == [] and state.last_mains_online is None
