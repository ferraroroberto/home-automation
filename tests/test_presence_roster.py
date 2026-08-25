"""Auto-arm must refuse a roster it cannot fully account for (issue #689).

The 2026-08-25 incident: ``config/presence_state.json`` lost one of two
tracked people, the survivor left the house, and "everyone away past grace"
became true with the missing person asleep upstairs. Nothing in the engine
could tell a household that shrank from a household that was always small.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src import presence_engine as P
from src import presence_roster as R


def _person(person_id: str, state: str, at: datetime) -> P.PersonPresence:
    return P.PersonPresence(person_id=person_id, state=state, updated_at=at)


def _cfg(**kwargs) -> P.PresenceAutomationConfig:
    base = dict(
        auto_arm_enabled=True,
        auto_disarm_enabled=True,
        arm_away_after_s=60,
        stale_after_s=3600,
    )
    base.update(kwargs)
    return P.PresenceAutomationConfig(**base)


# --- the roster store -------------------------------------------------------


def test_roster_is_empty_when_absent(tmp_path):
    assert R.load_roster(tmp_path / "presence_roster.json") == ()


def test_remember_people_unions_and_sorts(tmp_path):
    target = tmp_path / "presence_roster.json"
    assert R.remember_people(["roberto"], target) == ("roberto",)
    assert R.remember_people(["ana", "roberto"], target) == ("ana", "roberto")
    assert json.loads(target.read_text(encoding="utf-8")) == ["ana", "roberto"]


def test_remember_people_never_shrinks_the_roster(tmp_path):
    """The whole point: a state file that lost someone must not un-know them."""

    target = tmp_path / "presence_roster.json"
    R.remember_people(["roberto", "ana"], target)
    assert R.remember_people(["ana"], target) == ("ana", "roberto")


def test_remember_people_does_not_write_when_nothing_is_new(tmp_path):
    target = tmp_path / "presence_roster.json"
    R.remember_people(["roberto"], target)
    before = target.stat().st_mtime_ns
    target_bytes = target.read_bytes()

    R.remember_people(["roberto"], target)

    assert target.stat().st_mtime_ns == before
    assert target.read_bytes() == target_bytes


def test_roster_path_follows_the_state_path(tmp_path):
    """Redirecting presence state to a temp dir redirects the roster with it,
    so no test can write the real household's file by accident."""

    assert R.roster_path_for(tmp_path / "presence_state.json") == (
        tmp_path / "presence_roster.json"
    )


def test_set_person_state_registers_the_person(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    P.set_person_state("roberto", "home")
    assert R.load_roster(tmp_path / "presence_roster.json") == ("roberto",)


# --- the engine gate --------------------------------------------------------


def test_missing_roster_member_refuses_the_arm(monkeypatch, tmp_path):
    """The incident, reproduced: ana is away and alone in the state file, but
    the roster says roberto should be there too."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    assert (
        P.evaluate_alarm_decision(
            [_person("ana", "away", t0)],
            security_mode="disarmed",
            config=_cfg(),
            at=t0 + timedelta(seconds=70),
            known_person_ids=("ana", "roberto"),
        )
        is None
    )


def test_complete_roster_still_arms(monkeypatch, tmp_path):
    """The gate must not break the normal case it is guarding."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    decision = P.evaluate_alarm_decision(
        [_person("ana", "away", t0), _person("roberto", "away", t0)],
        security_mode="disarmed",
        config=_cfg(),
        at=t0 + timedelta(seconds=70),
        known_person_ids=("ana", "roberto"),
    )
    assert decision is not None and decision.kind == "arm"


def test_missing_roster_member_refuses_the_disarm_too(monkeypatch, tmp_path):
    """Refusing a disarm leaves the house armed, which is the safe way to be
    wrong about who is in it — so the gate is deliberately symmetric."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    assert (
        P.evaluate_alarm_decision(
            [_person("ana", "home", t0)],
            security_mode="armed",
            config=_cfg(),
            at=t0 + timedelta(seconds=5),
            known_person_ids=("ana", "roberto"),
        )
        is None
    )


def test_an_unknown_extra_person_does_not_block(monkeypatch, tmp_path):
    """The roster is a floor, not a whitelist — someone new reporting in is
    fine, and is registered by the next tick anyway."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    decision = P.evaluate_alarm_decision(
        [_person("ana", "away", t0), _person("guest", "away", t0)],
        security_mode="disarmed",
        config=_cfg(),
        at=t0 + timedelta(seconds=70),
        known_person_ids=("ana",),
    )
    assert decision is not None and decision.kind == "arm"


def test_empty_roster_preserves_pre_fix_behaviour(monkeypatch, tmp_path):
    """A household that has never populated a roster must behave exactly as it
    did before, so the change can't silently disable anyone's automation."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    decision = P.evaluate_alarm_decision(
        [_person("ana", "away", t0)],
        security_mode="disarmed",
        config=_cfg(),
        at=t0 + timedelta(seconds=70),
    )
    assert decision is not None and decision.kind == "arm"


def test_satisfied_disarm_key_refuses_while_someone_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    assert (
        P.satisfied_disarm_key(
            [_person("ana", "home", t0)],
            security_mode="disarmed",
            config=_cfg(),
            at=t0 + timedelta(seconds=5),
            known_person_ids=("ana", "roberto"),
        )
        is None
    )


# --- the diagnostic ---------------------------------------------------------


def test_missing_person_is_reported_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    block = P.evaluate_staleness_block(
        [_person("ana", "away", t0)],
        config=_cfg(),
        at=t0,
        known_person_ids=("ana", "roberto"),
    )
    assert block is not None
    assert block.missing_person_ids == ("roberto",)
    assert block.stale_person_ids == ()
    assert block.all_person_ids == ("roberto",)


def test_a_wholly_wiped_state_file_is_the_loudest_case(monkeypatch, tmp_path):
    """No people at all with a non-empty roster used to be filed as "nobody is
    configured" and reported nothing. It is the worst case there is."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    block = P.evaluate_staleness_block(
        [], config=_cfg(), at=t0, known_person_ids=("ana", "roberto")
    )
    assert block is not None
    assert block.missing_person_ids == ("ana", "roberto")


def test_stale_and_missing_are_reported_together(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    block = P.evaluate_staleness_block(
        [_person("ana", "away", t0 - timedelta(hours=5))],
        config=_cfg(stale_after_s=60),
        at=t0,
        known_person_ids=("ana", "roberto"),
    )
    assert block is not None
    assert block.stale_person_ids == ("ana",)
    assert block.missing_person_ids == ("roberto",)
    assert block.all_person_ids == ("ana", "roberto")


def test_complete_fresh_roster_reports_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    assert (
        P.evaluate_staleness_block(
            [_person("ana", "away", t0), _person("roberto", "home", t0)],
            config=_cfg(),
            at=t0,
            known_person_ids=("ana", "roberto"),
        )
        is None
    )


def test_arm_block_defers_to_the_missing_diagnostic(monkeypatch, tmp_path):
    """"ana is still home" would name the wrong blocker while the engine has
    lost sight of roberto entirely."""

    monkeypatch.setattr(P, "STATE_PATH", tmp_path / "presence_state.json")
    t0 = datetime(2026, 8, 25, 3, 40, 49, tzinfo=timezone.utc)

    assert (
        P.evaluate_arm_block(
            [_person("ana", "home", t0), _person("guest", "away", t0)],
            security_mode="disarmed",
            config=_cfg(),
            at=t0,
            known_person_ids=("ana", "guest", "roberto"),
        )
        is None
    )
