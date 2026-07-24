"""Unit tests for the webhook-backed presence transition engine."""

from datetime import datetime, timedelta, timezone

from src import presence_engine as P


def _person(person_id: str, state: str, at: datetime) -> P.PersonPresence:
    return P.PersonPresence(person_id=person_id, state=state, updated_at=at)


def test_everyone_away_after_grace_arms(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
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
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    assert P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=4),
    ) is None


def test_first_fresh_arrival_disarms_when_armed(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, stale_after_s=3600)
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
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, stale_after_s=60)
    assert P.evaluate_alarm_decision(
        [_person("roberto", "home", t0)],
        security_mode="armed",
        config=cfg,
        at=t0 + timedelta(minutes=2),
    ) is None


def test_kids_home_override_arms_perimeter(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
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
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, stale_after_s=3600)
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


def test_same_state_ping_keeps_state_since(monkeypatch, tmp_path):
    # A repeated same-state webhook refreshes the heartbeat but must NOT advance
    # the transition timestamp — otherwise the alarm keys churn.
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)
    P.set_person_state("roberto", "home", at=t0)
    P.set_person_state("roberto", "home", at=t0 + timedelta(minutes=10))
    person = P.load_people()["roberto"]
    assert person.updated_at == t0 + timedelta(minutes=10)  # heartbeat moved
    assert person.state_since == t0                          # transition did not
    # A real state change resets state_since.
    P.set_person_state("roberto", "away", at=t0 + timedelta(minutes=20))
    assert P.load_people()["roberto"].state_since == t0 + timedelta(minutes=20)


def test_scheduled_arm_not_undone_when_people_already_home(monkeypatch, tmp_path):
    # The reported bug: an 11pm perimeter arm was disarmed ~1s later because
    # people were home. A scheduled/manual arm AFTER everyone is already home must
    # stick, even as same-state pings keep arriving.
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    home_since = datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc)
    armed_at = datetime(2026, 6, 26, 23, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, stale_after_s=36000)
    P.note_manual_alarm_action("perimeter", at=armed_at)  # the 11pm schedule arm
    person = P.PersonPresence(
        person_id="roberto", state="home",
        updated_at=armed_at + timedelta(seconds=2),  # a fresh ping just after arm
        state_since=home_since,
    )
    assert P.evaluate_alarm_decision(
        [person], security_mode="perimeter", config=cfg,
        at=armed_at + timedelta(seconds=2),
    ) is None
    # But a genuine morning arrival (state_since advances past the arm) disarms.
    arrival = P.PersonPresence(
        person_id="roberto", state="home",
        updated_at=armed_at + timedelta(hours=8),
        state_since=armed_at + timedelta(hours=8),
    )
    decision = P.evaluate_alarm_decision(
        [arrival], security_mode="perimeter", config=cfg,
        at=armed_at + timedelta(hours=8, seconds=5),
    )
    assert decision is not None and decision.kind == "disarm"


def test_manual_action_after_transition_suppresses(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=0, stale_after_s=3600)
    P.note_manual_alarm_action("disarm", at=t0 + timedelta(minutes=1))
    assert P.evaluate_alarm_decision(
        [_person("roberto", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=2),
    ) is None


def test_auto_arm_disabled_suppresses_arm_even_with_disarm_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=False, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600,
    )
    assert P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
    ) is None


def test_auto_disarm_disabled_suppresses_disarm_even_with_arm_enabled(monkeypatch, tmp_path):
    # The reported real-world case (issue #516): a repair person was home while
    # everyone tracked was away, so auto-arm-on-departure was kept off, but
    # auto-disarm-on-arrival needed to keep working independently.
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, auto_disarm_enabled=False, stale_after_s=3600,
    )
    assert P.evaluate_alarm_decision(
        [_person("roberto", "home", t0), _person("ana", "away", t0 - timedelta(minutes=30))],
        security_mode="armed",
        config=cfg,
        at=t0 + timedelta(seconds=5),
    ) is None


def test_load_automation_config_migrates_legacy_both_off(monkeypatch, tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"enabled": false, "disarm_on_arrival": true}', encoding="utf-8")
    cfg = P.load_automation_config(path)
    assert cfg.auto_arm_enabled is False
    assert cfg.auto_disarm_enabled is False


def test_load_automation_config_migrates_legacy_both_on(monkeypatch, tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"enabled": true, "disarm_on_arrival": true}', encoding="utf-8")
    cfg = P.load_automation_config(path)
    assert cfg.auto_arm_enabled is True
    assert cfg.auto_disarm_enabled is True


def test_load_automation_config_migrates_legacy_arm_only(monkeypatch, tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"enabled": true, "disarm_on_arrival": false}', encoding="utf-8")
    cfg = P.load_automation_config(path)
    assert cfg.auto_arm_enabled is True
    assert cfg.auto_disarm_enabled is False


def test_load_automation_config_new_shape_round_trips(monkeypatch, tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"auto_arm_enabled": false, "auto_disarm_enabled": true}', encoding="utf-8")
    cfg = P.load_automation_config(path)
    assert cfg.auto_arm_enabled is False
    assert cfg.auto_disarm_enabled is True
