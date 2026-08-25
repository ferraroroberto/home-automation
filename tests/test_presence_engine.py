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


def test_guardian_home_holds_instead_of_arming(monkeypatch, tmp_path):
    """Issue #693: a guardian (e.g. "Nonna") home and fresh must hold the arm
    entirely — not even perimeter — overriding the kids-home toggle too."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
        override_perimeter=True,  # kids-home toggle also on — guardian must still win
        guardian_home_name="Nonna",
    )
    assert decision is not None
    assert decision.kind == "guardian_hold"
    assert decision.action == "hold"
    assert "Nonna" in decision.reason


def test_guardian_hold_is_edge_triggered_once_per_episode(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    people = [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))]
    at = t0 + timedelta(minutes=6)

    first = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg, at=at, guardian_home_name="Nonna",
    )
    assert first is not None and first.kind == "guardian_hold"
    P.mark_decision_applied(first, "held")

    again = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg,
        at=at + timedelta(minutes=1), guardian_home_name="Nonna",
    )
    assert again is None


def test_guardian_leaving_still_lets_the_same_episode_arm(monkeypatch, tmp_path):
    """The guardian_hold key must live in its own namespace — consuming it
    must never mark the underlying 'everyone away' arm key as applied, or the
    house could never arm for that departure once the guardian leaves."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    people = [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))]
    at = t0 + timedelta(minutes=6)

    held = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg, at=at, guardian_home_name="Nonna",
    )
    P.mark_decision_applied(held, "held")

    # Guardian has now left / gone stale — the same departure episode arms.
    now_armable = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg,
        at=at + timedelta(minutes=2), guardian_home_name=None,
    )
    assert now_armable is not None
    assert now_armable.kind == "arm"
    assert now_armable.action == "arm"


def test_stale_guardian_read_falls_back_to_arming(monkeypatch, tmp_path):
    """A >=24h-old guardian read must not be trusted — presence_automation's
    ``_resolve_guardian_home`` is what enforces the bound and would simply
    pass ``guardian_home_name=None`` through in that case; this confirms the
    engine arms normally whenever no (fresh) guardian name is supplied."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
        guardian_home_name=None,
    )
    assert decision is not None
    assert decision.kind == "arm"


def test_reproduces_2026_08_25_real_departure_with_guardian_home(monkeypatch, tmp_path):
    """Reproduction (issue #693): replays today's real
    config/presence_automation.json data — Roberto away 06:08:39Z, Ana away
    06:08:42Z, kids_home_override True — which really armed 'perimeter' at
    06:09:57Z while Nonna was in fact still home. With a synthetic fresh
    Nonna-at-home signal, the fix must hold instead of arming any mode."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    roberto_away = datetime.fromisoformat("2026-08-25T06:08:39.514530+00:00")
    ana_away = datetime.fromisoformat("2026-08-25T06:08:42.772446+00:00")
    people = [
        _person("roberto", "away", roberto_away),
        _person("ana", "away", ana_away),
    ]
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, auto_disarm_enabled=True,
        arm_away_after_s=60, stale_after_s=216000,
    )
    at = datetime.fromisoformat("2026-08-25T06:09:57.617832+00:00")

    # Old behavior, unchanged: kids-home override armed perimeter for real.
    old_decision = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg, at=at, override_perimeter=True,
    )
    assert old_decision is not None
    assert old_decision.kind == "arm"
    assert old_decision.action == "perimeter"

    # New behavior: Nonna's (synthetic) fresh at-home read holds the arm
    # instead, even with the kids-home override still on.
    new_decision = P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=cfg, at=at,
        override_perimeter=True, guardian_home_name="Nonna",
    )
    assert new_decision is not None
    assert new_decision.kind == "guardian_hold"
    assert new_decision.action == "hold"


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

    # dwell_s defaults to 0, i.e. notify is due on first observation (pre-#599 shape).
    assert P.set_arm_block(block) == P.ArmBlockObservation(changed=True, notify=True)
    assert P.load_arm_block() == {
        "blocked": True,
        "person_ids": ["ana"],
        "since": since.isoformat(),
    }
    # A confirmed send is what latches "already notified" (#601) - set_arm_block
    # itself never marks it just because a send was due.
    P.mark_arm_block_notified(block.key)
    # Same episode again - not a new observation, and already notified.
    assert P.set_arm_block(block) == P.ArmBlockObservation(changed=False, notify=False)
    # Clearing is itself a new observation, but never a notification.
    assert P.set_arm_block(None) == P.ArmBlockObservation(changed=True, notify=False)
    assert P.load_arm_block() == {"blocked": False, "person_ids": [], "since": None}
    # Already clear - not new.
    assert P.set_arm_block(None) == P.ArmBlockObservation(changed=False, notify=False)


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


# --- arm-block notification dwell (issue #599) ---
#
# Production incident 2026-08-01: ana's arrival webhook landed at 17:51:07 and
# roberto's at 17:51:39. The block diagnostic fired a Telegram "FAILED" at
# 17:51:27 - inside the 32 s gap between two people walking in together - and
# cleared itself 12 s later. Nothing had failed; nothing was even attempted.


_ANA_IN = datetime(2026, 8, 1, 17, 51, 7, 924162, tzinfo=timezone.utc)
_ROB_IN = datetime(2026, 8, 1, 17, 51, 39, 885749, tzinfo=timezone.utc)
_ALERTED_AT = datetime(2026, 8, 1, 17, 51, 27, 903472, tzinfo=timezone.utc)


def _ana_block() -> P.PresenceBlock:
    return P.PresenceBlock(
        key=f"block:ana:{_ANA_IN.isoformat()}",
        blocking_person_ids=("ana",),
        since=_ANA_IN,
    )


def test_two_arrivals_32s_apart_do_not_notify(monkeypatch, tmp_path):
    """The exact production window: no notification inside the arrival gap."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()

    first = P.set_arm_block(block, dwell_s=900, at=_ANA_IN + timedelta(seconds=10))
    assert first.changed is True      # still worth logging + showing in the UI
    assert first.notify is False      # ...but not worth paging about

    # The tick that actually paged in production.
    assert P.set_arm_block(block, dwell_s=900, at=_ALERTED_AT).notify is False

    # roberto arrives; the block evaporates on its own.
    cleared = P.set_arm_block(None, dwell_s=900, at=_ROB_IN)
    assert cleared.changed is True and cleared.notify is False


def test_block_notifies_once_after_dwell(monkeypatch, tmp_path):
    """A genuinely stuck presence still pages - once - after the dwell."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()

    assert P.set_arm_block(block, dwell_s=900, at=_ANA_IN).notify is False
    assert P.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=899)
    ).notify is False
    assert P.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True
    # A confirmed send (#601) is what latches "already notified" - without it
    # the episode would keep reporting `notify=True` forever.
    P.mark_arm_block_notified(block.key)
    # And not again on later ticks of the same episode.
    for extra in (901, 1200, 7200):
        assert P.set_arm_block(
            block, dwell_s=900, at=_ANA_IN + timedelta(seconds=extra)
        ).notify is False


# --- a send that's due but not (yet) confirmed sent (issue #601) ---
#
# #527 fixed the per-day error de-dupe to only latch once a Telegram send was
# *confirmed* delivered. The arm-block path had the same eager-latch bug:
# `set_arm_block` used to mark `arm_blocked_notified` the moment a send became
# due, regardless of whether the caller's send actually went through - a
# failed/unconfigured notifier silently burned the episode's one-and-only
# alert with `block.key` staying stable for as long as the presence itself
# stayed stuck.


def test_notify_due_is_not_marked_notified_until_confirmed(monkeypatch, tmp_path):
    """A send that never happens (declined, failed, no notifier) must not burn
    the episode's alert - `set_arm_block` alone must never latch `notified`."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    P.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen

    at = _ANA_IN + timedelta(seconds=900)
    assert P.set_arm_block(block, dwell_s=900, at=at).notify is True
    # Nothing marked it delivered - still due on a later tick, once the retry
    # cooldown (untouched here) has elapsed.
    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=P._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_failed_attempt_backs_off_instead_of_retrying_every_tick(monkeypatch, tmp_path):
    """A declined/failed send must retry on a *later* tick, not every ~10s
    poll tick - `mark_arm_block_attempted` is what backs this off."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    P.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen

    at = _ANA_IN + timedelta(seconds=900)
    assert P.set_arm_block(block, dwell_s=900, at=at).notify is True
    P.mark_arm_block_attempted(block.key, at=at)  # attempted, but not confirmed sent

    # A tick shortly after (the ~10s presence poll cadence) must not retry yet.
    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=10)
    ).notify is False
    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=P._ARM_BLOCK_RETRY_COOLDOWN_S - 1)
    ).notify is False
    # Once the cooldown window elapses, a retry is due again.
    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=P._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_mark_arm_block_notified_ignores_a_stale_key(monkeypatch, tmp_path):
    """A confirmed send for an episode that has since moved on must not mark
    the wrong (current) episode as notified."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    P.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen
    at = _ANA_IN + timedelta(seconds=900)
    P.set_arm_block(block, dwell_s=900, at=at)

    P.mark_arm_block_notified("some-other-episode-key")

    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=P._ARM_BLOCK_RETRY_COOLDOWN_S)
    ).notify is True


def test_mark_arm_block_attempted_ignores_a_stale_key(monkeypatch, tmp_path):
    """An attempt timestamp for a superseded episode must not throttle the
    current one."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    P.set_arm_block(block, dwell_s=900, at=_ANA_IN)  # establishes first_seen
    at = _ANA_IN + timedelta(seconds=900)
    P.set_arm_block(block, dwell_s=900, at=at)

    P.mark_arm_block_attempted("some-other-episode-key", at=at)

    # The (misdirected) attempt must not have started this episode's cooldown.
    assert P.set_arm_block(
        block, dwell_s=900, at=at + timedelta(seconds=1)
    ).notify is True


def test_dwell_is_anchored_to_first_seen_not_to_since(monkeypatch, tmp_path):
    """`since` can be hours old for someone legitimately home all day.

    Anchoring the dwell to it would fire instantly the moment anyone else left
    - reintroducing the very false alert this dwell exists to prevent.
    """
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    home_all_day = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    other_leaves = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)  # 9 h later
    block = P.PresenceBlock(
        key=f"block:ana:{home_all_day.isoformat()}",
        blocking_person_ids=("ana",),
        since=home_all_day,
    )

    assert P.set_arm_block(block, dwell_s=900, at=other_leaves).notify is False
    assert P.set_arm_block(
        block, dwell_s=900, at=other_leaves + timedelta(seconds=900)
    ).notify is True


def test_new_episode_restarts_the_dwell(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    first = _ana_block()
    P.set_arm_block(first, dwell_s=900, at=_ANA_IN)
    assert P.set_arm_block(
        first, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True

    # A different set of blocking people is a different episode.
    second = P.PresenceBlock(
        key="block:ana,roberto:x", blocking_person_ids=("ana", "roberto"), since=_ANA_IN,
    )
    at = _ANA_IN + timedelta(seconds=1000)
    assert P.set_arm_block(second, dwell_s=900, at=at).notify is False
    assert P.set_arm_block(
        second, dwell_s=900, at=at + timedelta(seconds=900)
    ).notify is True


def test_recurring_block_after_clear_notifies_again(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = _ana_block()
    P.set_arm_block(block, dwell_s=900, at=_ANA_IN)
    assert P.set_arm_block(
        block, dwell_s=900, at=_ANA_IN + timedelta(seconds=900)
    ).notify is True
    P.set_arm_block(None, dwell_s=900, at=_ANA_IN + timedelta(hours=1))

    back = _ANA_IN + timedelta(hours=2)
    assert P.set_arm_block(block, dwell_s=900, at=back).notify is False
    assert P.set_arm_block(
        block, dwell_s=900, at=back + timedelta(seconds=900)
    ).notify is True


def test_block_is_visible_in_the_api_before_the_dwell_elapses(monkeypatch, tmp_path):
    """The dwell gates the *notification*, not the diagnostic (#599)."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    P.set_arm_block(_ana_block(), dwell_s=900, at=_ANA_IN + timedelta(seconds=10))
    assert P.load_arm_block() == {
        "blocked": True,
        "person_ids": ["ana"],
        "since": _ANA_IN.isoformat(),
    }


def test_arm_block_dwell_config_round_trips(tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"arm_block_notify_after_s": 120}', encoding="utf-8")
    assert P.load_automation_config(path).arm_block_notify_after_s == 120
    # Absent / malformed -> the default dwell, never an instant page.
    for body in ('{}', '{"arm_block_notify_after_s": null}', '{"arm_block_notify_after_s": "x"}'):
        path.write_text(body, encoding="utf-8")
        assert P.load_automation_config(path).arm_block_notify_after_s == 900


# --- iCloud staleness corroboration (issue #653) ---
#
# Production incident 2026-08-11: roberto's "leave home" webhook fired
# correctly, but auto-arm never fired because ana's webhook record hadn't
# updated in ~4 days (she simply hadn't crossed her geofence) - the freshness
# gate silently refused every decision for the whole household, with zero
# trace. Corroboration lets a fresher, agreeing iCloud/Find My read stand in
# for the missing webhook heartbeat.


def test_icloud_corroboration_window_config_round_trips(tmp_path):
    path = tmp_path / "presence_automation.json"
    path.write_text('{"icloud_corroboration_window_s": 3600}', encoding="utf-8")
    assert P.load_automation_config(path).icloud_corroboration_window_s == 3600
    for body in ('{}', '{"icloud_corroboration_window_s": null}', '{"icloud_corroboration_window_s": "x"}'):
        path.write_text(body, encoding="utf-8")
        assert P.load_automation_config(path).icloud_corroboration_window_s == 21600


def test_stale_webhook_arms_when_icloud_corroboration_agrees(monkeypatch, tmp_path):
    """The exact production shape: ana's webhook is stale, but her iCloud
    entity is fresh and agrees she's away - the arm must still fire."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))  # far past stale_after_s
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=False),
    }
    decision = P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    )
    assert decision is not None
    assert decision.kind == "arm"


def test_stale_webhook_still_blocks_when_icloud_also_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        # Also outside the corroboration window - can't vouch for her either.
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(days=2), at_home=False),
    }
    assert P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


def test_stale_webhook_still_blocks_when_icloud_disagrees(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(
        auto_arm_enabled=True, arm_away_after_s=60, stale_after_s=3600,
        icloud_corroboration_window_s=21600,
    )
    ana_stale = _person("ana", "away", t0 - timedelta(days=4))
    roberto_left = _person("roberto", "away", t0)
    corroboration = {
        # Fresh, but says she's actually home - disagrees with the stale webhook.
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=True),
    }
    assert P.evaluate_alarm_decision(
        [ana_stale, roberto_left],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


def test_fresh_webhook_is_unaffected_by_absent_corroboration(monkeypatch, tmp_path):
    """A person with no corroboration entry at all (no linked iCloud entity)
    is unaffected as long as their webhook data is itself fresh."""
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, arm_away_after_s=300, stale_after_s=3600)
    decision = P.evaluate_alarm_decision(
        [_person("roberto", "away", t0), _person("ana", "away", t0 + timedelta(seconds=30))],
        security_mode="disarmed",
        config=cfg,
        at=t0 + timedelta(minutes=6),
        corroboration={},
    )
    assert decision is not None and decision.kind == "arm"


def test_evaluate_staleness_block_fires_when_uncorroborated(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    block = P.evaluate_staleness_block(
        [_person("ana", "away", t0 - timedelta(days=4)), _person("roberto", "away", t0)],
        config=cfg,
        at=t0 + timedelta(minutes=5),
    )
    assert block is not None
    assert block.stale_person_ids == ("ana",)


def test_evaluate_staleness_block_does_not_fire_when_corroborated(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    corroboration = {
        "ana": P.PresenceCorroboration(last_seen=t0 - timedelta(hours=1), at_home=False),
    }
    assert P.evaluate_staleness_block(
        [_person("ana", "away", t0 - timedelta(days=4)), _person("roberto", "away", t0)],
        config=cfg,
        at=t0 + timedelta(minutes=5),
        corroboration=corroboration,
    ) is None


def test_evaluate_staleness_block_does_not_fire_when_everyone_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    cfg = P.PresenceAutomationConfig(auto_arm_enabled=True, stale_after_s=3600)
    assert P.evaluate_staleness_block(
        [_person("ana", "away", t0), _person("roberto", "away", t0)],
        config=cfg,
        at=t0,
    ) is None


def test_set_staleness_block_notifies_once_after_dwell(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    block = P.StalePresenceBlock(key="stale:ana", stale_person_ids=("ana",))

    assert P.set_staleness_block(block, dwell_s=900, at=t0).notify is False
    assert P.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=899)
    ).notify is False
    assert P.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=900)
    ).notify is True
    P.mark_staleness_block_notified(block.key)
    assert P.set_staleness_block(
        block, dwell_s=900, at=t0 + timedelta(seconds=1200)
    ).notify is False

    # Independent state namespace: the arm-block diagnostic must be unaffected.
    assert P.load_arm_block() == {"blocked": False, "person_ids": [], "since": None}
    assert P.load_staleness_block() == {"blocked": True, "person_ids": ["ana"]}


def test_set_staleness_block_clears(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    block = P.StalePresenceBlock(key="stale:ana", stale_person_ids=("ana",))
    P.set_staleness_block(block, dwell_s=0)
    assert P.load_staleness_block()["blocked"] is True

    cleared = P.set_staleness_block(None)
    assert cleared.changed is True and cleared.notify is False
    assert P.load_staleness_block() == {"blocked": False, "person_ids": []}


# --- write churn on the shared state file (issue #689) ---
#
# Both block diagnostics ran every ~10s tick and saved unconditionally, so
# `presence_state.json` was rewritten four times a tick to persist bytes
# identical to the ones just read. That churn is what kept the file permanently
# mid-`os.replace` and made a concurrent reader's sharing violation - the read
# that came back empty and wiped the roster - near-certain.


def test_unchanged_block_tick_does_not_rewrite_the_state_file(monkeypatch, tmp_path):
    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    since = datetime(2026, 8, 25, 3, 40, tzinfo=timezone.utc)
    block = P.PresenceBlock(
        key="block:roberto:" + since.isoformat(),
        blocking_person_ids=("roberto",),
        since=since,
    )

    P.set_arm_block(block, dwell_s=900, at=since)
    settled = state.read_bytes()
    mtime = state.stat().st_mtime_ns

    # Same episode, same tick shape, nothing new to record.
    P.set_arm_block(block, dwell_s=900, at=since + timedelta(seconds=10))
    P.set_arm_block(block, dwell_s=900, at=since + timedelta(seconds=20))

    assert state.read_bytes() == settled
    assert state.stat().st_mtime_ns == mtime


def test_an_already_clear_block_does_not_rewrite_the_state_file(monkeypatch, tmp_path):
    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    P.set_arm_block(None)
    settled = state.read_bytes()
    mtime = state.stat().st_mtime_ns

    P.set_arm_block(None)
    P.set_arm_block(None)

    assert state.read_bytes() == settled
    assert state.stat().st_mtime_ns == mtime


def test_a_real_block_change_still_persists(monkeypatch, tmp_path):
    """The skip must not swallow an episode that genuinely moved."""

    state = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", state)
    since = datetime(2026, 8, 25, 3, 40, tzinfo=timezone.utc)
    first = P.PresenceBlock(key="block:ana", blocking_person_ids=("ana",), since=since)
    second = P.PresenceBlock(
        key="block:roberto", blocking_person_ids=("roberto",), since=since
    )

    P.set_arm_block(first, dwell_s=900, at=since)
    assert P.load_arm_block()["person_ids"] == ["ana"]
    assert P.set_arm_block(second, dwell_s=900, at=since).changed is True
    assert P.load_arm_block()["person_ids"] == ["roberto"]
