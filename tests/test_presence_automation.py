"""Unit tests for app.webapp.presence_automation.tick()'s arm/disarm apply path.

Covers routing the presence-triggered arm/disarm through the shared
``confirm_alarm_action`` retry helper (issue #390) - this is the actual gap
that caused a real false alarm: RISCO's WebUI call rejected a presence-
triggered "arm", but the panel confirmed armed shortly after, and this path
had zero retry/backoff before alerting (issue #388 only covered the schedule
engine). The retry/backoff mechanics themselves are covered in
``tests/test_alarm_notify.py``; these tests only confirm tick() routes
through the shared helper instead of calling ``control_system`` directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import app.webapp.presence_automation as PA
from src.presence_engine import PresenceDecision
from src.risco_client import RiscoCommandError


class _FakeSecurity:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.ongoing_alarm = False
        self.memory_alarm = False
        self.ac_lost = False


class _FakeConfirmState:
    def __init__(self, mode: str) -> None:
        self.mode = mode


class _Config:
    enabled = True


_DECISION = PresenceDecision(
    kind="arm",
    action="arm",
    key="arm:2026-07-06T15:00:00+00:00",
    reason="everyone away past grace",
    transition_at=datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc),
)


def _wire_common(monkeypatch) -> None:
    async def fake_fetch_security_state() -> _FakeSecurity:
        return _FakeSecurity("disarmed")

    async def fake_check_security_transitions(**kw) -> None:
        pass

    monkeypatch.setattr(PA, "fetch_security_state", fake_fetch_security_state)
    monkeypatch.setattr(PA, "check_security_transitions", fake_check_security_transitions)
    monkeypatch.setattr(PA, "consider_security_read", lambda security: None)
    monkeypatch.setattr(PA, "consider_security_override", lambda security: None)
    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"p1": object()})
    monkeypatch.setattr(PA, "evaluate_alarm_decision", lambda *a, **k: _DECISION)
    monkeypatch.setattr(PA, "load_kids_home_override", lambda: False)
    monkeypatch.setattr(PA, "send_push", lambda *a, **k: None)
    monkeypatch.setattr(PA, "append_trigger_log", lambda event: None)


def test_presence_tick_applies_arm_via_confirm_helper_and_records_ok(monkeypatch) -> None:
    recorded: list[dict] = []
    applied: list[tuple] = []
    _wire_common(monkeypatch)

    async def fake_confirm(action: str) -> _FakeConfirmState:
        assert action == "arm"
        return _FakeConfirmState("armed")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "mark_decision_applied", lambda d, o: applied.append((d, o)))
    monkeypatch.setattr(PA, "set_kids_home_override", lambda v: None)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA.tick())

    assert recorded == [
        {
            "source": PA.SOURCE_PRESENCE,
            "action": "arm",
            "outcome": PA.OUTCOME_OK,
            "detail": _DECISION.reason,
        }
    ]
    assert applied == [(_DECISION, "armed")]


def test_presence_tick_alerts_only_after_confirm_helper_exhausts_retries(monkeypatch) -> None:
    """Regression for today's real false alarm: presence-triggered arm/disarm
    previously had zero retry and alerted on the very first raised exception.
    Now the failure only reaches here after ``confirm_alarm_action`` has
    exhausted its own read-only backoff retries."""

    recorded: list[dict] = []
    _wire_common(monkeypatch)

    async def fake_confirm(action: str) -> _FakeConfirmState:
        raise RiscoCommandError("RISCO rejected 'arm': D:")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(PA, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(PA, "record_alarm_action", fake_record_alarm_action)

    asyncio.run(PA.tick())

    assert recorded == [
        {
            "source": PA.SOURCE_PRESENCE,
            "action": "arm",
            "outcome": PA.OUTCOME_ERROR,
            "error": "RISCO rejected 'arm': D:",
            "detail": _DECISION.reason,
            "dedupe_key": "presence:arm",
        }
    ]


def test_evaluate_current_decision_considers_every_tracked_person(monkeypatch) -> None:
    """Regression for #490: a person hidden from the Presence UI list (a
    display-only declutter toggle in ``config/presence_hidden.json``) must
    still be evaluated by the arm/disarm decision, not silently dropped -
    hiding one of two tracked people previously made the automation blind to
    them while still acting on the other.
    """

    captured: dict = {}

    def fake_evaluate(people, **kwargs):
        captured["people"] = list(people)
        return None

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {"ana": object(), "roberto": object()})
    monkeypatch.setattr(PA, "evaluate_alarm_decision", fake_evaluate)
    monkeypatch.setattr(PA, "load_kids_home_override", lambda: False)

    PA._evaluate_current_decision("disarmed")

    assert len(captured["people"]) == 2


def test_evaluate_current_decision_warns_loudly_when_no_people_tracked(
    monkeypatch, caplog
) -> None:
    """Regression for #490: an empty people list after loading must not be a
    silent no-op - it needs a visible log line so it's diagnosable."""

    monkeypatch.setattr(PA, "load_automation_config", lambda: _Config())
    monkeypatch.setattr(PA, "load_people", lambda: {})

    with caplog.at_level(logging.WARNING, logger=PA.logger.name):
        result = PA._evaluate_current_decision("disarmed")

    assert result is None
    assert any("no tracked people" in record.message for record in caplog.records)
