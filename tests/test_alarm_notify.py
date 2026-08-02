"""Unit tests for the alarm activity-log + Telegram notification wiring.

Covers :mod:`src.activity_log`, :mod:`src.alarm_notify_prefs`, and the
:func:`app.webapp.alarm_notify.record_alarm_action` policy: manual never
notifies, automatic notifies only when its toggle is on, errors use the
``error`` toggle, a missing notifier / delivery failure is a safe no-op, and a
keyed error de-dupes to once per local day while still logging every attempt.
No network, no real config files (all redirected to ``tmp_path``).
"""

from __future__ import annotations

import asyncio
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
from src.risco_client import RiscoCommandError


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
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_MANUAL,
        action="arm",
        outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(schedule_arm=True, error=True),
        notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []
    rows = _read_log(tmp_path)
    assert rows[0]["source"] == "manual" and rows[0]["event"] == "set"
    assert "actor" not in rows[0]  # omitted when not passed — backward compatible


def test_manual_records_actor_when_provided(tmp_path: Path, monkeypatch) -> None:
    """issue #405 — distinguishes webapp/ha/voice-pe callers in logs/alarm.jsonl."""
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    for actor in ("webapp", "ha", "voice-pe"):
        asyncio.run(AN.record_alarm_action(
            source=AN.SOURCE_MANUAL,
            action="arm",
            outcome=AN.OUTCOME_OK,
            actor=actor,
            prefs_loader=lambda: AlarmNotifyPrefs(),
            notifier_factory=lambda: notifier,
        ))
    rows = _read_log(tmp_path)
    assert [row["actor"] for row in rows] == ["webapp", "ha", "voice-pe"]
    assert notifier.sent == []  # actor tagging never changes the manual no-notify policy


def test_automatic_success_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    # presence_arm off → logged, not sent.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_PRESENCE, action="arm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(presence_arm=False),
        notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []
    # presence_arm on → sent.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_PRESENCE, action="arm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(presence_arm=True),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1
    assert "armed" in notifier.sent[0]
    assert len(_read_log(tmp_path)) == 2  # both attempts logged


def test_error_uses_error_toggle_and_carries_text(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="RISCO rejected 'arm': D:",
        prefs_loader=lambda: AlarmNotifyPrefs(error=True, schedule_arm=False),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1
    assert "FAILED" in notifier.sent[0]
    assert "RISCO rejected 'arm': D:" in notifier.sent[0]


def test_no_notifier_is_safe_noop(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="disarm", outcome=AN.OUTCOME_OK,
        prefs_loader=lambda: AlarmNotifyPrefs(schedule_disarm=True),
        notifier_factory=lambda: None,
    ))
    assert _read_log(tmp_path)[0]["event"] == "unset"  # logged, no crash


def test_delivery_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    # Must not raise even though the notifier blows up.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="boom",
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: BoomNotifier(),
    ))
    assert _read_log(tmp_path)[0]["outcome"] == "error"


def test_error_dedupes_once_per_day_but_logs_every_attempt(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    notifier = FakeNotifier()
    day = datetime(2026, 6, 29, 7, 0, 0)
    for _ in range(3):
        asyncio.run(AN.record_alarm_action(
            source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
            error="panel offline", dedupe_key="schedule:weekday", now=day,
            prefs_loader=lambda: AlarmNotifyPrefs(error=True),
            notifier_factory=lambda: notifier,
        ))
    # One notification despite three failed retries...
    assert len(notifier.sent) == 1
    # ...but every attempt is in the activity log (so retry count is visible).
    assert len(_read_log(tmp_path)) == 3
    # De-dupe state persisted to disk.
    dedupe = json.loads((tmp_path / "alarm_notify_dedupe.json").read_text())
    assert dedupe == {"schedule:weekday": "2026-06-29"}

    # A new day re-arms the alert.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="panel offline", dedupe_key="schedule:weekday",
        now=datetime(2026, 6, 30, 7, 0, 0),
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 2


def test_failed_delivery_does_not_burn_the_days_dedupe(tmp_path: Path, monkeypatch) -> None:
    """#527: the dedupe marker must only be written after a *confirmed*
    successful send. Before the fix it was written unconditionally, so a
    delivery failure (or an unconfigured notifier) silently ate the day's
    only retry with no way to recover."""

    _redirect_logs(monkeypatch, tmp_path)
    day = datetime(2026, 7, 25, 5, 4, 0)
    dedupe_path = tmp_path / "alarm_notify_dedupe.json"

    # First attempt: delivery fails.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="disarm", outcome=AN.OUTCOME_ERROR,
        error="panel read back 'perimeter' after disarm, not the expected state",
        dedupe_key="schedule:schedule-mqrvx3dd", now=day,
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: BoomNotifier(),
    ))
    assert not dedupe_path.exists()  # nothing delivered yet — not marked as sent

    # Later the same day: delivery succeeds — must still go through.
    notifier = FakeNotifier()
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="disarm", outcome=AN.OUTCOME_ERROR,
        error="panel read back 'perimeter' after disarm, not the expected state",
        dedupe_key="schedule:schedule-mqrvx3dd", now=day,
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1
    dedupe = json.loads(dedupe_path.read_text())
    assert dedupe == {"schedule:schedule-mqrvx3dd": "2026-07-25"}

    # A third attempt the same day is now correctly suppressed.
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="disarm", outcome=AN.OUTCOME_ERROR,
        error="panel read back 'perimeter' after disarm, not the expected state",
        dedupe_key="schedule:schedule-mqrvx3dd", now=day,
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1


def test_unconfigured_notifier_does_not_burn_the_days_dedupe(tmp_path: Path, monkeypatch) -> None:
    _redirect_logs(monkeypatch, tmp_path)
    asyncio.run(AN.record_alarm_action(
        source=AN.SOURCE_SCHEDULE, action="arm", outcome=AN.OUTCOME_ERROR,
        error="panel offline", dedupe_key="schedule:weekday",
        now=datetime(2026, 7, 25, 5, 4, 0),
        prefs_loader=lambda: AlarmNotifyPrefs(error=True),
        notifier_factory=lambda: None,
    ))
    assert not (tmp_path / "alarm_notify_dedupe.json").exists()


def test_successful_send_logs_info_breadcrumb(tmp_path: Path, monkeypatch, caplog) -> None:
    """#527: a successful Telegram send must leave a log trace — before the
    fix, only a failed send was ever logged, so a successful delivery was
    indistinguishable from a silent no-op just by reading the logs."""

    _redirect_logs(monkeypatch, tmp_path)
    with caplog.at_level("INFO", logger="app.webapp.alarm_notify"):
        asyncio.run(AN.record_alarm_action(
            source=AN.SOURCE_PRESENCE, action="arm", outcome=AN.OUTCOME_OK,
            prefs_loader=lambda: AlarmNotifyPrefs(presence_arm=True),
            notifier_factory=lambda: FakeNotifier(),
        ))
    assert any("Telegram alarm notification sent" in r.message for r in caplog.records)


# ------------------------------------------- panel events (intrusion / ac_lost)


def test_security_transitions_baseline_then_intrusion_onset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    prefs = lambda: AlarmNotifyPrefs(intrusion=True, ac_lost=True)
    tracker = {"intrusion": None, "ac_lost": None}

    # First observation sets the baseline — no alert even though ac_lost is True.
    asyncio.run(AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []

    # Intrusion goes false→true → exactly one 🚨 alert.
    asyncio.run(AN.check_security_transitions(
        intrusion=True, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1 and "TRIGGERED" in notifier.sent[0]

    # Intrusion clearing is not an alert.
    asyncio.run(AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1


def test_security_transitions_ignores_unreadable_intrusion_poll(tmp_path: Path, monkeypatch) -> None:
    """An unreadable WebUI scrape (``intrusion=None``) must not be read as "cleared".

    Regression for issue #307: a transient scrape hiccup returning ``None``
    was mistaken for the alarm clearing, so the *next* successful poll
    re-observing a still-latched, days-old ``memory_alarm`` manufactured a
    bogus false→true "new" intrusion and paged for nothing.
    """

    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    prefs = lambda: AlarmNotifyPrefs(intrusion=True, ac_lost=True)
    tracker = {"intrusion": True, "ac_lost": True}  # already latched from a prior real onset

    asyncio.run(AN.check_security_transitions(
        intrusion=None, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []
    assert tracker["intrusion"] is True  # left untouched, not reset

    # The still-latched flag reasserting itself as True must not re-fire.
    asyncio.run(AN.check_security_transitions(
        intrusion=True, ac_lost=True, state=tracker,
        prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []


def test_intrusion_log_carries_diagnostic_flags_but_telegram_stays_clean(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    tracker = {"intrusion": False, "ac_lost": None}

    asyncio.run(AN.check_security_transitions(
        intrusion=True, ac_lost=False,
        intrusion_detail="ongoing_alarm=False memory_alarm=True",
        state=tracker,
        prefs_loader=lambda: AlarmNotifyPrefs(intrusion=True),
        notifier_factory=lambda: notifier,
    ))
    rows = _read_log(tmp_path)
    assert rows[0]["diagnostic"] == "ongoing_alarm=False memory_alarm=True"
    assert len(notifier.sent) == 1
    assert "ongoing_alarm" not in notifier.sent[0]  # diagnostic stays log-only


def test_security_tracker_persists_across_restart_and_logs_baseline(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """#527: the intrusion/ac_lost tracker used to be in-memory only, so every
    tray restart re-hit the "first observation" no-alert branch — silently
    forgetting an already-active condition each time. It must now survive a
    simulated restart (a fresh module-level dict reloaded from disk), and the
    first-ever observation must still be visible in the log even though it
    doesn't alert."""

    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    state_path = tmp_path / "alarm_security_state.json"
    monkeypatch.setattr(AN, "_SECURITY_STATE_PATH", state_path)
    monkeypatch.setattr(AN, "_last_security", {"intrusion": None, "ac_lost": None})
    notifier = FakeNotifier()
    prefs = lambda: AlarmNotifyPrefs(intrusion=True, ac_lost=True)

    # First-ever observation: already True (e.g. a still-latched alarm).
    # Baseline only — no alert — but it must leave a log breadcrumb.
    with caplog.at_level("INFO", logger="app.webapp.alarm_notify"):
        asyncio.run(AN.check_security_transitions(
            intrusion=True, ac_lost=False, prefs_loader=prefs, notifier_factory=lambda: notifier,
        ))
    assert notifier.sent == []
    assert any("baseline set: intrusion=True" in r.message for r in caplog.records)
    assert json.loads(state_path.read_text()) == {"intrusion": True, "ac_lost": False}

    # Simulate a tray restart: fresh in-memory dict, reloaded from disk.
    monkeypatch.setattr(AN, "_last_security", AN._load_security_state())
    assert AN._last_security == {"intrusion": True, "ac_lost": False}

    # A genuinely new intrusion (already True -> stays True) must not spam,
    # but clearing then re-triggering after the restart still alerts once.
    asyncio.run(AN.check_security_transitions(
        intrusion=False, ac_lost=False, prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    asyncio.run(AN.check_security_transitions(
        intrusion=True, ac_lost=False, prefs_loader=prefs, notifier_factory=lambda: notifier,
    ))
    assert len(notifier.sent) == 1 and "TRIGGERED" in notifier.sent[0]


def test_security_ac_lost_alerts_both_directions_and_respects_toggle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    notifier = FakeNotifier()
    tracker = {"intrusion": False, "ac_lost": False}  # baseline already set

    # ac_lost off → no alert on the transition.
    asyncio.run(AN.check_security_transitions(
        intrusion=False, ac_lost=True, state=dict(tracker),
        prefs_loader=lambda: AlarmNotifyPrefs(ac_lost=False),
        notifier_factory=lambda: notifier,
    ))
    assert notifier.sent == []

    # ac_lost on → both loss and restore alert.
    on = lambda: AlarmNotifyPrefs(ac_lost=True)
    asyncio.run(AN.check_security_transitions(intrusion=False, ac_lost=True, state=tracker,
                                  prefs_loader=on, notifier_factory=lambda: notifier))
    asyncio.run(AN.check_security_transitions(intrusion=False, ac_lost=False, state=tracker,
                                  prefs_loader=on, notifier_factory=lambda: notifier))
    assert len(notifier.sent) == 2
    assert "lost mains" in notifier.sent[0]
    assert "restored" in notifier.sent[1]


class _FakeState:
    """Minimal stand-in for ``SecurityState`` - only ``mode`` is read."""

    def __init__(self, mode: str) -> None:
        self.mode = mode


def test_action_took_effect_confirms_matching_arm_and_disarm() -> None:
    assert AN.action_took_effect("arm", _FakeState("armed")) is True
    assert AN.action_took_effect("disarm", _FakeState("disarmed")) is True


def test_action_took_effect_flags_arm_and_disarm_mismatch() -> None:
    assert AN.action_took_effect("arm", _FakeState("disarmed")) is False
    assert AN.action_took_effect("arm", _FakeState("partial")) is False
    assert AN.action_took_effect("disarm", _FakeState("armed")) is False


def test_action_took_effect_treats_partial_and_perimeter_as_interchangeable() -> None:
    assert AN.action_took_effect("partial", _FakeState("partial")) is True
    assert AN.action_took_effect("partial", _FakeState("perimeter")) is True
    assert AN.action_took_effect("perimeter", _FakeState("partial")) is True
    assert AN.action_took_effect("perimeter", _FakeState("perimeter")) is True
    assert AN.action_took_effect("partial", _FakeState("armed")) is False


# --------------------------------------------------------- confirm_alarm_action
#
# Shared by the schedule engine (app.webapp.security_automation) and the
# presence automation (app.webapp.presence_automation) - both retry a failed
# confirmation identically via this one helper (issues #388, #390).


def test_confirm_alarm_action_succeeds_immediately(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        calls.append(action)
        return _FakeState("armed")

    async def fail_fetch() -> _FakeState:
        raise AssertionError("fetch_security_state must not be called on immediate success")

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fail_fetch)

    state = asyncio.run(AN.confirm_alarm_action("arm"))

    assert state.mode == "armed"
    assert calls == ["arm"]


def test_confirm_alarm_action_confirms_on_first_readonly_recheck_without_resend(monkeypatch) -> None:
    """A mismatch that clears by the first backoff's read-only re-check needs
    no resend at all - the command already went through, just with a lag."""

    control_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        control_calls.append(action)
        return _FakeState("perimeter")  # not yet disarmed

    async def fake_fetch() -> _FakeState:
        fetch_calls.append("fetch")
        return _FakeState("disarmed")  # confirmed on the first read-only retry

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fake_fetch)
    monkeypatch.setattr(AN, "CONFIRM_RETRY_DELAYS_S", (0, 0, 0))

    state = asyncio.run(AN.confirm_alarm_action("disarm"))

    assert state.mode == "disarmed"
    assert control_calls == ["disarm"]  # never resent - the read-only check already confirmed it
    assert len(fetch_calls) == 1


def test_confirm_alarm_action_resends_command_when_readonly_recheck_still_fails(monkeypatch) -> None:
    """Issue #390 (revised): a read-only recheck alone isn't enough for a
    genuinely dropped command - if the state still doesn't confirm after a
    backoff wait, resend the command before the next wait."""

    control_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        control_calls.append(action)
        # First (initial) issue doesn't take; the resend (2nd call) does.
        return _FakeState("perimeter" if len(control_calls) < 2 else "disarmed")

    async def fake_fetch() -> _FakeState:
        fetch_calls.append("fetch")
        return _FakeState("perimeter")  # still not confirmed at the first recheck

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fake_fetch)
    monkeypatch.setattr(AN, "CONFIRM_RETRY_DELAYS_S", (0, 0, 0))

    state = asyncio.run(AN.confirm_alarm_action("disarm"))

    assert state.mode == "disarmed"
    assert control_calls == ["disarm", "disarm"]  # initial issue + one resend
    assert len(fetch_calls) == 1  # exactly one read-only recheck before the resend


def test_confirm_alarm_action_retries_after_raised_exception_and_succeeds(monkeypatch) -> None:
    """Regression for the real false alarm: RISCO's own WebUI call raised
    ('RISCO rejected 'arm'') on the first attempt, yet the panel confirmed
    armed shortly after - the first read-only re-check catches that."""

    control_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        control_calls.append(action)
        raise RiscoCommandError("RISCO rejected 'arm': D:")

    async def fake_fetch() -> _FakeState:
        fetch_calls.append("fetch")
        return _FakeState("armed")

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fake_fetch)
    monkeypatch.setattr(AN, "CONFIRM_RETRY_DELAYS_S", (0, 0, 0))

    state = asyncio.run(AN.confirm_alarm_action("arm"))

    assert state.mode == "armed"
    assert control_calls == ["arm"]  # confirmed by the read-only recheck, no resend needed
    assert len(fetch_calls) == 1


def test_confirm_alarm_action_confirms_on_final_readonly_check_after_two_resends(monkeypatch) -> None:
    """The full worst-almost-case: two resends fail to confirm immediately,
    and the state only comes around on the very last (120s) read-only check -
    which must not trigger a third resend, since there are no retries left."""

    control_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        control_calls.append(action)
        return _FakeState("disarmed")  # neither the initial issue nor the resends confirm

    async def fake_fetch() -> _FakeState:
        fetch_calls.append("fetch")
        # Confirmed only on the third (last) read-only recheck.
        return _FakeState("armed" if len(fetch_calls) == 3 else "disarmed")

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fake_fetch)
    monkeypatch.setattr(AN, "CONFIRM_RETRY_DELAYS_S", (0, 0, 0))

    state = asyncio.run(AN.confirm_alarm_action("arm"))

    assert state.mode == "armed"
    assert control_calls == ["arm", "arm", "arm"]  # initial + resend after check 1 + resend after check 2
    assert len(fetch_calls) == 3


def test_confirm_alarm_action_raises_after_exhausting_all_retries(monkeypatch) -> None:
    control_calls: list[str] = []
    fetch_calls: list[str] = []

    async def fake_control(action: str) -> _FakeState:
        control_calls.append(action)
        return _FakeState("disarmed")  # never matches "arm"

    async def fake_fetch() -> _FakeState:
        fetch_calls.append("fetch")
        return _FakeState("disarmed")

    monkeypatch.setattr(AN, "control_system", fake_control)
    monkeypatch.setattr(AN, "fetch_security_state", fake_fetch)
    monkeypatch.setattr(AN, "CONFIRM_RETRY_DELAYS_S", (0, 0, 0))

    try:
        asyncio.run(AN.confirm_alarm_action("arm"))
        raised = False
    except RiscoCommandError as exc:
        raised = True
        assert "disarmed" in str(exc)

    assert raised is True
    # Initial issue + a resend after each of the first two failed rechecks
    # (the last recheck, at the final backoff, gives up instead of resending).
    assert control_calls == ["arm", "arm", "arm"]
    assert len(fetch_calls) == 3  # one read-only recheck per backoff delay


# --- blocked vs failed wording (issue #599) ---


def test_blocked_outcome_does_not_read_as_a_failed_command() -> None:
    """A block means nothing was attempted, so it must not say FAILED.

    The 2026-08-01 report was a user seeing "Automatic alarm arm FAILED" 20 s
    after a correct auto-disarm and reasonably concluding a command had failed.
    """
    blocked = AN._compose_message(
        AN.SOURCE_PRESENCE, "arm", AN.OUTCOME_BLOCKED,
        "ana still reported home since 2026-08-01T17:51:07.924162+00:00", None,
    )
    assert "FAILED" not in blocked
    assert "on hold" in blocked
    assert "ana still reported home since" in blocked

    # A genuine command failure is untouched.
    failed = AN._compose_message(
        AN.SOURCE_PRESENCE, "arm", AN.OUTCOME_ERROR, "RISCO rejected 'arm': D:", None,
    )
    assert "FAILED" in failed


def test_blocked_outcome_rides_the_error_toggle_and_dedupe() -> None:
    """Reuses #533's existing plumbing rather than adding a notify path."""
    on = AN.AlarmNotifyPrefs(error=True)
    off = AN.AlarmNotifyPrefs(error=False)
    assert AN._should_notify(on, AN.SOURCE_PRESENCE, "arm", AN.OUTCOME_BLOCKED) is True
    assert AN._should_notify(off, AN.SOURCE_PRESENCE, "arm", AN.OUTCOME_BLOCKED) is False
