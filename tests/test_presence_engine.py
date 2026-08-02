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


# --- evaluate_arm_block / set_arm_block / load_arm_block (issue #531) ---


def test_arm_block_fires_when_one_person_still_home(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 7, 25, 20, 21, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, arm_away_after_s=300, stale_after_s=216000)
    block = P.evaluate_arm_block(
        [_person("ana", "home", t0), _person("roberto", "away", t0 + timedelta(hours=14))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(hours=14, minutes=6),
    )
    assert block is not None
    assert block.blocking_person_ids == ("ana",)
    assert block.since == t0


def test_arm_block_does_not_fire_when_everyone_home(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    assert P.evaluate_arm_block(
        [_person("ana", "home", t0), _person("roberto", "home", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0,
    ) is None


def test_arm_block_does_not_fire_when_everyone_away(monkeypatch, tmp_path):
    # Would arm outright once past grace - not a block, so no diagnostic.
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    assert P.evaluate_arm_block(
        [_person("ana", "away", t0), _person("roberto", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0,
    ) is None


def test_arm_block_does_not_fire_when_auto_arm_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=False, stale_after_s=3600)
    assert P.evaluate_arm_block(
        [_person("ana", "home", t0), _person("roberto", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0,
    ) is None


def test_arm_block_does_not_fire_when_not_disarmed(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    assert P.evaluate_arm_block(
        [_person("ana", "home", t0), _person("roberto", "away", t0)],
        security_mode="armed",
        config=cfg,
        at=t0,
    ) is None


def test_arm_block_does_not_fire_when_blocker_is_stale(monkeypatch, tmp_path):
    # Staleness is already its own (existing) silent case - don't double-report.
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=60)
    assert P.evaluate_arm_block(
        [_person("ana", "home", t0), _person("roberto", "away", t0)],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
    ) is None


def test_set_arm_block_persists_and_reports_new_episode(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    since = datetime(2026, 7, 25, 20, 21, tzinfo=timezone.utc)
    block = P.PresenceBlock(key="block:ana:" + since.isoformat(), blocking_person_ids=("ana",), since=since)

    assert P.set_arm_block(block) is True
    assert P.load_arm_block() == {
        "blocked": True,
        "person_ids": ["ana"],
        "since": since.isoformat(),
    }
    # Same episode again - not a new observation.
    assert P.set_arm_block(block) is False
    # Clearing is itself a new observation.
    assert P.set_arm_block(None) is True
    assert P.load_arm_block() == {"blocked": False, "person_ids": [], "since": None}
    # Already clear - not new.
    assert P.set_arm_block(None) is False


# --- stale disarm decisions (issue #598) ---
#
# Production incident 2026-08-01: ana arrived 17:51:07Z and the panel disarmed
# correctly; roberto arrived 17:51:39Z while it was *already* disarmed, so that
# newer key was never recorded; at 20:43Z the panel was armed by hand from the
# keypad and the 2h52m-old arrival auto-disarmed it.


_ANA_ARRIVAL = datetime(2026, 8, 1, 17, 51, 7, 924162, tzinfo=timezone.utc)
_ROB_ARRIVAL = datetime(2026, 8, 1, 17, 51, 39, 885749, tzinfo=timezone.utc)
_KEYPAD_ARM = datetime(2026, 8, 1, 20, 43, 25, tzinfo=timezone.utc)


def _at_home(person_id: str, since: datetime, *, seen: datetime) -> P.PersonPresence:
    return P.PersonPresence(
        person_id=person_id, state="home", updated_at=seen, state_since=since,
    )


def _incident_config(**overrides) -> P.PresenceAutomationConfig:
    base = {
        "auto_arm_enabled": True,
        "auto_disarm_enabled": True,
        "stale_after_s": 36000,  # long, so freshness never masks the assertion
    }
    base.update(overrides)
    return P.PresenceAutomationConfig(**base)


def test_arrival_while_already_disarmed_is_consumed(monkeypatch, tmp_path):
    """Fix 1: the moot key is recorded, so it cannot fire on a later arm.

    Deliberately kept *inside* the freshness bound (the arm happens 5 min after
    the arrival) so this test fails if only the age bound is implemented.
    """
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config()

    # Step 1 - ana arrives, panel armed, disarm fires and is applied.
    decision = P.evaluate_alarm_decision(
        [_at_home("ana", _ANA_ARRIVAL, seen=_ANA_ARRIVAL),
         _person("roberto", "away", _ANA_ARRIVAL - timedelta(hours=8))],
        security_mode="armed", config=cfg, at=_ANA_ARRIVAL + timedelta(seconds=9),
    )
    assert decision is not None and decision.kind == "disarm"
    P.mark_decision_applied(decision, "disarmed")

    # Step 2 - roberto arrives 32 s later; the panel is already disarmed, so
    # there is nothing to do, but the newer key must be consumed.
    people = [_at_home("ana", _ANA_ARRIVAL, seen=_ROB_ARRIVAL),
              _at_home("roberto", _ROB_ARRIVAL, seen=_ROB_ARRIVAL)]
    at_step2 = _ROB_ARRIVAL + timedelta(seconds=5)
    assert P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg, at=at_step2,
    ) is None
    satisfied = P.satisfied_disarm_key(
        people, security_mode="disarmed", config=cfg, at=at_step2,
    )
    assert satisfied == f"disarm:{_ROB_ARRIVAL.isoformat()}"
    P.mark_disarm_satisfied(satisfied)

    # Step 3 - the panel is armed by hand 5 minutes later (well within the
    # freshness bound, and invisible to `_manual_after`). Nothing may fire.
    armed_at = _ROB_ARRIVAL + timedelta(minutes=5)
    assert P.evaluate_alarm_decision(
        [_at_home("ana", _ANA_ARRIVAL, seen=armed_at),
         _at_home("roberto", _ROB_ARRIVAL, seen=armed_at)],
        security_mode="armed", config=cfg, at=armed_at + timedelta(seconds=25),
    ) is None


def test_disarm_refused_when_transition_older_than_max_age(monkeypatch, tmp_path):
    """Fix 2: the age bound, exercised with the moot key deliberately NOT consumed.

    This is the belt-and-braces path - a webapp restart, a rolled-back
    presence_state.json, any route that leaves a key pending.
    """
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config(disarm_max_age_s=900)
    assert P.evaluate_alarm_decision(
        [_at_home("ana", _ANA_ARRIVAL, seen=_KEYPAD_ARM),
         _at_home("roberto", _ROB_ARRIVAL, seen=_KEYPAD_ARM)],
        security_mode="armed", config=cfg, at=_KEYPAD_ARM,
    ) is None


def test_disarm_allowed_just_inside_max_age(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config(disarm_max_age_s=900)
    at = _ROB_ARRIVAL + timedelta(seconds=899)
    decision = P.evaluate_alarm_decision(
        [_at_home("roberto", _ROB_ARRIVAL, seen=at)],
        security_mode="armed", config=cfg, at=at,
    )
    assert decision is not None and decision.kind == "disarm"


def test_disarm_max_age_zero_disables_the_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config(disarm_max_age_s=0)
    decision = P.evaluate_alarm_decision(
        [_at_home("roberto", _ROB_ARRIVAL, seen=_KEYPAD_ARM)],
        security_mode="armed", config=cfg, at=_KEYPAD_ARM,
    )
    assert decision is not None and decision.kind == "disarm"


def test_satisfied_disarm_key_none_when_panel_not_disarmed(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config()
    assert P.satisfied_disarm_key(
        [_at_home("roberto", _ROB_ARRIVAL, seen=_ROB_ARRIVAL)],
        security_mode="armed", config=cfg, at=_ROB_ARRIVAL,
    ) is None


def test_satisfied_disarm_key_none_when_already_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config()
    people = [_at_home("roberto", _ROB_ARRIVAL, seen=_ROB_ARRIVAL)]
    key = P.satisfied_disarm_key(
        people, security_mode="disarmed", config=cfg, at=_ROB_ARRIVAL,
    )
    assert key is not None
    P.mark_disarm_satisfied(key)
    assert P.satisfied_disarm_key(
        people, security_mode="disarmed", config=cfg, at=_ROB_ARRIVAL,
    ) is None


def test_satisfied_disarm_key_none_when_nobody_home(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config()
    assert P.satisfied_disarm_key(
        [_person("roberto", "away", _ROB_ARRIVAL)],
        security_mode="disarmed", config=cfg, at=_ROB_ARRIVAL,
    ) is None


def test_satisfied_disarm_key_none_when_auto_disarm_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    cfg = _incident_config(auto_disarm_enabled=False)
    assert P.satisfied_disarm_key(
        [_at_home("roberto", _ROB_ARRIVAL, seen=_ROB_ARRIVAL)],
        security_mode="disarmed", config=cfg, at=_ROB_ARRIVAL,
    ) is None


def test_arm_path_is_not_age_bounded(monkeypatch, tmp_path):
    """The asymmetry is deliberate: bounding the arm path would leave an empty
    house unarmed after any downtime longer than the bound (issue #598)."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    cfg = _incident_config(arm_away_after_s=900, disarm_max_age_s=900)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0)],
        security_mode="disarmed", config=cfg, at=t0 + timedelta(hours=6),
    )
    assert decision is not None and decision.kind == "arm"


def test_automation_config_round_trips_disarm_max_age(tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"auto_disarm_enabled": true, "disarm_max_age_s": 300}', encoding="utf-8")
    assert P.load_automation_config(path).disarm_max_age_s == 300
    # Absent -> the safe default, not unbounded.
    path.write_text('{"auto_disarm_enabled": true}', encoding="utf-8")
    assert P.load_automation_config(path).disarm_max_age_s == 900


def test_disarm_max_age_falls_back_to_bounded_default_on_garbage(tmp_path):
    """A null/garbage value must not read as 'unbounded' — that is the unsafe
    direction for a safety bound (issue #598)."""
    path = tmp_path / "presence_automation.json"
    for bad in ('null', '"soon"', '[]'):
        path.write_text(
            '{"auto_disarm_enabled": true, "disarm_max_age_s": %s}' % bad, encoding="utf-8"
        )
        assert P.load_automation_config(path).disarm_max_age_s == 900
