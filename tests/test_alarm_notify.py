"""Unit tests for the alarm activity-log + Telegram notification wiring.

Covers :mod:`src.activity_log`, :mod:`src.alarm_notify_prefs`, and the
:func:`app.webapp.alarm_notify.record_alarm_action` policy: manual never
notifies, automatic notifies only when its toggle is on, errors use the
``error`` toggle, a missing notifier / delivery failure is a safe no-op, and a
keyed error de-dupes to once per local day while still logging every attempt.
No network, no real config files (all redirected to ``tmp_path``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List

import app.webapp.alarm_notify as AN
from src import activity_log
from src.alarm_notify_prefs import (
    AlarmNotifyPrefs,
    load_alarm_notify_prefs,
    save_alarm_notify_prefs,
)
from src.notify import NotifierError


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


class BoomNotifier:
    def send_text(self, text: str) -> None:
        raise NotifierError("delivery boom")


def _read_log(tmp_path: Path) -> List[dict]:
    path = tmp_path / "alarm.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _redirect_logs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(AN, "_DEDUPE_PATH", tmp_path / "alarm_notify_dedupe.json")
    AN._last_error_notify.clear()


# --------------------------------------------------------------- activity_log


def test_append_activity_injects_ts_and_consumer(tmp_path: Path) -> None:
    activity_log.append_activity("alarm", {"action": "arm"}, path=tmp_path / "alarm.jsonl")
    rows = _read_log(tmp_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "arm"
    assert rows[0]["consumer"] == "alarm"
    assert rows[0]["ts"]  # an ISO timestamp was stamped in


def test_append_activity_preserves_caller_fields(tmp_path: Path) -> None:
    activity_log.append_activity(
        "presence", {"consumer": "alarm", "ts": "fixed"}, path=tmp_path / "x.jsonl"
    )
    row = json.loads((tmp_path / "x.jsonl").read_text(encoding="utf-8").strip())
    # setdefault must not clobber values the caller already supplied.
    assert row["consumer"] == "alarm"
    assert row["ts"] == "fixed"


# ----------------------------------------------------------------- prefs store


def test_prefs_default_is_error_only(tmp_path: Path) -> None:
    prefs = load_alarm_notify_prefs(tmp_path / "absent.json")
    assert prefs == AlarmNotifyPrefs(error=True)
    assert prefs.error is True
    assert prefs.schedule_arm is False and prefs.presence_disarm is False


def test_prefs_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    save_alarm_notify_prefs(AlarmNotifyPrefs(schedule_arm=True, error=False), path)
    loaded = load_alarm_notify_prefs(path)
    assert loaded.schedule_arm is True
    assert loaded.error is False
    assert not (tmp_path / "prefs.json.tmp").exists()  # atomic write left no sidecar


# ----------------------------------------------- notifier factory safety net


def test_build_alarm_notifier_is_none_under_pytest(monkeypatch) -> None:
    """The default notifier_factory must never build a real notifier in tests.

    record_alarm_action / record_power_event default notifier_factory to
    build_alarm_notifier, and a default argument binds at def time, so a test
    that forgets to inject a fake notifier would otherwise send a real Telegram
    alert. The choke-point guard makes that impossible even with live creds. (#273)
    """

    from src import notify_config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:real-looking-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    assert notify_config.is_notify_configured() is True  # creds resolve...
    assert notify_config.build_alarm_notifier() is None  # ...but no notifier under pytest


# ------------------------------------------------------- record_alarm_action


def test_manual_logs_but_never_notifies(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    # Even with every toggle on, a manual source must not push.
    AN.record_alarm_action(
        source=AN.SOURCE_MANUAL,
        action="arm",
        outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(schedule_arm=True, error=True),
        notifier_factory=lambda: notifier,
    )
    assert notifier.sent == []
    rows = _read_log(tmp_path)
    assert rows[0]["source"] == "manual" and rows[0]["event"] == "set"


def test_automatic_success_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    # presence_arm off → logged, not sent.
    AN.record_alarm_action(
        source=AN.SOURCE_PRESENCE, action="arm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(presence_arm=False),
        notifier_factory=lambda: notifier,
    )
    assert notifier.sent == []
    # presence_arm on → sent.
    AN.record_alarm_action(
        source=AN.SOURCE_PRESENCE, action="arm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(presence_arm=True),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 1
    assert "armed" in notifier.sent[0]
    assert len(_read_log(tmp_path)) == 2  # both attempts logged


def test_error_uses_error_toggle_and_carries_text(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="RISCO rejected 'arm': D:",
        prefs_loader=lambda: AlarmNotifyPrefs(error=True, schedule_arm=False),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 1
    assert "FAILED" in notifier.sent[0]
    assert "RISCO rejected 'arm': D:" in notifier.sent[0]


def test_no_notifier_is_safe_noop(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="disarm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(schedule_disarm=True),
        notifier_factory=lambda: None,
    )
    assert _read_log(tmp_path)[0]["event"] == "unset"  # logged, no crash


def test_delivery_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    # Must not raise even though the notifier blows up.
    AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="boom",
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: BoomNotifier(),
    )
    assert _read_log(tmp_path)[0]["outcome"] == "error"


def test_error_dedupes_once_per_day_but_logs_every_attempt(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    day = datetime(2026, 6, 29, 7, 0, 0)
    for _ in range(3):
        AN.record_alarm_action(
            source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
            error="panel offline", dedupe_key="schedule:weekday", now=day,
            prefs_loader=lambda: AlarmNotifyPrefs(error=True),
            notifier_factory=lambda: notifier,
        )
    # One notification despite three failed retries...
    assert len(notifier.sent) == 1
    # ...but every attempt is in the activity log (so retry count is visible).
    assert len(_read_log(tmp_path)) == 3
    # De-dupe state persisted to disk.
    dedupe = json.loads((tmp_path / "alarm_notify_dedupe.json").read_text())
    assert dedupe == {"schedule:weekday": "2026-06-29"}

    # A new day re-arms the alert.
    AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="panel offline", dedupe_key="schedule:weekday",
        now=datetime(2026, 6, 30, 7, 0, 0),
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 2


# ------------------------------------------- panel events (intrusion / ac_lost)


def test_security_transitions_baseline_then_intrusion_onset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    prefs = lambda: AlarmNotifyPrefs(intrusion=True, ac_lost=True)
    tracker = {"intrusion": None, "ac_lost": None}

    # First observation sets the baseline — no alert even though ac_lost is True.
    AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    )
    assert notifier.sent == []

    # Intrusion goes false→true → exactly one 🚨 alert.
    AN.check_security_transitions(
        intrusion=True, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 1 and "TRIGGERED" in notifier.sent[0]

    # Intrusion clearing is not an alert.
    AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    )
    assert len(notifier.sent) == 1


def test_security_ac_lost_alerts_both_directions_and_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    tracker = {"intrusion": False, "ac_lost": False}  # baseline already set

    # ac_lost off → no alert on the transition.
    AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=dict(tracker),
        prefs_loader=lambda: AlarmNotifyPrefs(ac_lost=False),
        notifier_factory=lambda: notifier,
    )
    assert notifier.sent == []

    # ac_lost on → both loss and restore alert.
    on = lambda: AlarmNotifyPrefs(ac_lost=True)
    AN.check_security_transitions(intrusion=False, ac_lost=True, state=tracker,
                                  prefs_loader=on, notifier_factory=lambda: notifier)
    AN.check_security_transitions(intrusion=False, ac_lost=False, state=tracker,
                                  prefs_loader=on, notifier_factory=lambda: notifier)
    assert len(notifier.sent) == 2
    assert "lost mains" in notifier.sent[0]
    assert "restored" in notifier.sent[1]
