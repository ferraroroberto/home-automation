"""Unit tests for the webhook-backed presence transition engine."""

from datetime import datetime, timedelta, timezone

from src import presence_engine as P


def _person(person_id: str, state: str, at: datetime) -> P.PersonPresence:
    return P.PersonPresence(person_id=person_id, state=state, updated_at=at)


def test_everyone_away_after_grace_arms(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
    )
    assert decision is not None
    assert decision.kind == "arm"
    assert decision.action == "arm"


def test_everyone_away_before_grace_holds(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, arm_away_after_s=300, stale_after_s=3600)
    assert P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=4),
    ) is None


def test_first_fresh_arrival_disarms_when_armed(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "home", t0), _person("ana", "away", t0 - timedelta(minutes=30))],
        security_mode="armed",
        config=cfg,
        at=t0 + timedelta(seconds=5),
    )
    assert decision is not None
    assert decision.kind == "disarm"
    assert decision.action == "disarm"


def test_stale_state_does_not_disarm(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, stale_after_s=60)
    assert P.evaluate_alarm_decision(
        [_person("roberto", "home", t0)],
        security_mode="armed",
        config=cfg,
        at=t0 + timedelta(minutes=2),
    ) is None


def test_kids_home_override_arms_perimeter(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
        override_perimeter=True,
    )
    assert decision is not None
    assert decision.kind == "arm"
    assert decision.action == "perimeter"


def test_kids_home_override_does_not_affect_disarm(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "home", t0), _person("ana", "away", t0 - timedelta(minutes=30))],
        security_mode="armed",
        config=cfg,
        at=t0 + timedelta(seconds=5),
        override_perimeter=True,
    )
    assert decision is not None
    assert decision.kind == "disarm"
    assert decision.action == "disarm"


def test_kids_home_override_persists_and_loads(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    assert P.load_kids_home_override() is False
    P.set_kids_home_override(True)
    assert P.load_kids_home_override() is True
    P.set_kids_home_override(False)
    assert P.load_kids_home_override() is False


def test_manual_action_after_transition_suppresses(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(enabled=True, arm_away_after_s=0, stale_after_s=3600)
    P.note_manual_alarm_action("disarm", at=t0 + timedelta(minutes=1))
    assert P.evaluate_alarm_decision(
        [_person("roberto", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=2),
    ) is None
