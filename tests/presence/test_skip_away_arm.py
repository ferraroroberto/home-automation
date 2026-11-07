"""The one-shot "Don't arm when away" override, engine and tick.

Security-critical: a mistake leaves the house unarmed. Everything here runs on
a tmp ``presence_state.json``, fake people and a fixed clock; the panel, the
blinds and Telegram are fakes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import app.webapp.presence_automation as PA
from src._schedule_store import StoreUnreadableError
from src.presence import presence_engine as P
from src.presence import presence_skip_away_arm as S

_LEFT = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
_NOW = _LEFT + timedelta(minutes=6)
_CFG = P.PresenceAutomationConfig(
    auto_arm_enabled=True, auto_disarm_enabled=True, arm_away_after_s=300, stale_after_s=3600
)


def _away(person_id: str, since: datetime = _LEFT) -> P.PersonPresence:
    return P.PersonPresence(person_id, "away", _NOW - timedelta(seconds=30), state_since=since)


def _home(person_id: str, since: datetime) -> P.PersonPresence:
    return P.PersonPresence(person_id, "home", _NOW - timedelta(seconds=30), state_since=since)


def _decide(people, at: datetime = _NOW, **kwargs):
    return P.evaluate_alarm_decision(
        people, security_mode="disarmed", config=_CFG, at=at, **kwargs
    )


@pytest.fixture
def state(monkeypatch, tmp_path):
    path = tmp_path / "presence_state.json"
    monkeypatch.setattr(P, "STATE_PATH", path)
    return path


# --- Engine -------------------------------------------------------------------


def test_override_on_turns_the_away_arm_into_a_skip_with_the_arm_key(state) -> None:
    decision = _decide([_away("roberto"), _away("ana")], skip_away_arm=True)

    assert decision is not None
    assert decision.kind == "skip_arm"
    assert decision.key == f"arm:{_LEFT.isoformat()}"


def test_override_off_leaves_the_arm_decision_unchanged(state) -> None:
    decision = _decide([_away("roberto"), _away("ana")], skip_away_arm=False)

    assert decision is not None
    assert (decision.kind, decision.action) == ("arm", "arm")
    assert decision.key == f"arm:{_LEFT.isoformat()}"


def test_guardian_hold_wins_over_the_override(state) -> None:
    decision = _decide([_away("roberto")], skip_away_arm=True, guardian_home_name="Nonna")

    assert decision is not None and decision.kind == "guardian_hold"


def test_no_skip_before_the_grace_period_is_over(state) -> None:
    early = _LEFT + timedelta(minutes=4)
    assert _decide([_away("roberto")], at=early, skip_away_arm=True) is None


def test_no_skip_after_a_manual_action_since_departure(state) -> None:
    P.note_manual_alarm_action("disarm", at=_LEFT + timedelta(minutes=1))
    assert _decide([_away("roberto")], skip_away_arm=True) is None


def test_no_skip_while_someone_is_home(state) -> None:
    people = [_away("roberto"), _home("ana", _LEFT - timedelta(hours=1))]
    assert _decide(people, skip_away_arm=True) is None


def test_recording_the_skip_clears_both_overrides_and_consumes_the_arm(state) -> None:
    P.set_kids_home_override(True)
    S.set_skip_away_arm_override(True)
    people = [_away("roberto"), _away("ana")]
    decision = _decide(people, skip_away_arm=S.load_skip_away_arm_override())

    kids_home_was_on = S.record_away_arm_skip(decision.key)

    assert kids_home_was_on is True
    assert S.load_skip_away_arm_override() is False
    assert P.load_kids_home_override() is False
    meta = json.loads(state.read_text(encoding="utf-8"))["automation"]
    assert meta["last_arm_key"] == decision.key
    assert meta["last_arm_outcome"] == "skipped"
    # Same departure, a tick later: nothing left to arm or skip.
    later = _NOW + timedelta(seconds=10)
    assert _decide(people, at=later, skip_away_arm=S.load_skip_away_arm_override()) is None


def test_the_next_departure_after_a_skip_arms_normally(state) -> None:
    S.set_skip_away_arm_override(True)
    first = _decide([_away("roberto")], skip_away_arm=S.load_skip_away_arm_override())
    S.record_away_arm_skip(first.key)

    left_again = _LEFT + timedelta(hours=3)
    at = left_again + timedelta(minutes=6)
    people = [P.PersonPresence("roberto", "away", at, state_since=left_again)]
    decision = _decide(people, at=at, skip_away_arm=S.load_skip_away_arm_override())

    assert decision is not None
    assert (decision.kind, decision.action) == ("arm", "arm")


def test_override_persists_and_loads(state) -> None:
    assert S.load_skip_away_arm_override() is False
    S.set_skip_away_arm_override(True)
    assert S.load_skip_away_arm_override() is True
    S.set_skip_away_arm_override(False)
    assert S.load_skip_away_arm_override() is False


# --- Tick ---------------------------------------------------------------------


class _FakeSecurity:
    mode = "disarmed"
    ongoing_alarm = False
    memory_alarm = False
    ac_lost = False


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is not None else _NOW.replace(tzinfo=None)


@pytest.fixture
def house(monkeypatch, state):
    """A tick wired to the real engine over a tmp store; everything else faked."""

    calls = {"confirm": [], "follow": [], "notify": [], "trigger": []}
    people = {"roberto": _away("roberto"), "ana": _away("ana")}

    async def fetch_security_state():
        return _FakeSecurity()

    async def noop(*a, **k):
        return None

    async def confirm(action):
        calls["confirm"].append(action)

        class _State:
            mode = "armed"

        return _State()

    async def follow(kind, action):
        calls["follow"].append((kind, action))

    async def notify(text, *, label):
        calls["notify"].append(text)
        return True

    monkeypatch.setattr(PA, "datetime", _FixedClock)
    monkeypatch.setattr(PA, "fetch_security_state", fetch_security_state)
    monkeypatch.setattr(PA, "check_security_transitions", noop)
    monkeypatch.setattr(PA, "consider_security_read", lambda s: None)
    monkeypatch.setattr(PA, "consider_security_override", lambda s: None)
    monkeypatch.setattr(PA, "_build_icloud_corroboration", lambda cache: {})
    monkeypatch.setattr(PA, "_resolve_guardian_home", lambda cache, cfg, at: None)
    monkeypatch.setattr(PA, "remember_known_people", lambda ids: tuple(ids))
    monkeypatch.setattr(PA, "_sync_arm_block_diagnostic", noop)
    monkeypatch.setattr(PA, "_sync_staleness_block_diagnostic", noop)
    monkeypatch.setattr(PA, "_consume_satisfied_disarm", lambda *a, **k: None)
    monkeypatch.setattr(PA, "load_automation_config", lambda: _CFG)
    monkeypatch.setattr(PA, "load_people", lambda: dict(people))
    monkeypatch.setattr(PA, "confirm_alarm_action", confirm)
    monkeypatch.setattr(PA, "record_alarm_action", noop)
    monkeypatch.setattr(PA, "send_push", lambda *a, **k: None)
    monkeypatch.setattr(PA, "follow_alarm", follow)
    monkeypatch.setattr(PA, "notify_best_effort", notify)
    monkeypatch.setattr(PA, "append_trigger_log", lambda e: calls["trigger"].append(e))
    monkeypatch.setattr(S, "_CLEAR_FAILURE", S._ClearFailure())
    calls["people"] = people
    return calls


def test_tick_skips_the_arm_clears_the_override_and_announces_it(house) -> None:
    S.set_skip_away_arm_override(True)

    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert house["follow"] == []
    assert S.load_skip_away_arm_override() is False
    assert len(house["notify"]) == 1
    assert "Don't arm when away" in house["notify"][0]
    assert "Kids home" not in house["notify"][0]
    assert [e["outcome"] for e in house["trigger"]] == ["skipped"]

    # The tick after: same departure, nothing happens.
    asyncio.run(PA.tick())
    assert house["confirm"] == []
    assert len(house["notify"]) == 1


def test_tick_with_both_overrides_arms_nothing_and_clears_both(house) -> None:
    P.set_kids_home_override(True)
    S.set_skip_away_arm_override(True)

    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert S.load_skip_away_arm_override() is False
    assert P.load_kids_home_override() is False
    assert "Kids home was reset too" in house["notify"][0]


def test_tick_with_the_override_off_arms_as_today(house) -> None:
    asyncio.run(PA.tick())

    assert house["confirm"] == ["arm"]
    assert house["notify"] == []


def test_an_unreadable_override_counts_as_off_and_the_house_arms(house, monkeypatch) -> None:
    def unreadable():
        raise StoreUnreadableError("presence_state.json unreadable")

    monkeypatch.setattr(PA, "load_skip_away_arm_override", unreadable)

    asyncio.run(PA.tick())

    assert house["confirm"] == ["arm"]


def test_a_skip_that_cannot_be_saved_arms_nothing_and_retries(house, monkeypatch) -> None:
    S.set_skip_away_arm_override(True)

    def unsaveable(key):
        raise StoreUnreadableError("presence_state.json unreadable")

    monkeypatch.setattr(PA, "record_away_arm_skip", unsaveable)
    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert house["notify"] == []
    assert S.load_skip_away_arm_override() is True

    # The store recovers: the next tick records the skip, still no arm.
    monkeypatch.setattr(PA, "record_away_arm_skip", S.record_away_arm_skip)
    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert S.load_skip_away_arm_override() is False
    assert len(house["notify"]) == 1


def test_override_switched_on_while_waiting_for_the_lock_sends_no_command(
    house, monkeypatch
) -> None:
    """The coordinated re-check inside the lock must never send a skip."""

    real_lock = PA.automatic_alarm_action_lock

    class _FlipOnEnter:
        def __init__(self):
            self._inner = real_lock()

        async def __aenter__(self):
            S.set_skip_away_arm_override(True)
            return await self._inner.__aenter__()

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

    monkeypatch.setattr(PA, "automatic_alarm_action_lock", _FlipOnEnter)

    asyncio.run(PA.tick())
    assert house["confirm"] == []

    # The next tick applies the skip.
    monkeypatch.setattr(PA, "automatic_alarm_action_lock", real_lock)
    asyncio.run(PA.tick())
    assert house["confirm"] == []
    assert S.load_skip_away_arm_override() is False


# --- Never outlives one away/return cycle ---------------------------------------

_SET_AT = _LEFT - timedelta(hours=2)


def test_tick_clears_the_override_on_the_first_arrival_after_it_was_set(house, caplog) -> None:
    """Set while away, someone comes home: off, so the next empty house arms."""

    S.set_skip_away_arm_override(True, at=_SET_AT)
    house["people"]["roberto"] = _home("roberto", _SET_AT + timedelta(hours=1))

    with caplog.at_level("INFO"):
        asyncio.run(PA.tick())

    assert S.load_skip_away_arm_override() is False
    assert house["confirm"] == []
    assert any("cleared" in r.getMessage() and "roberto" in r.getMessage() for r in caplog.records)

    # Everyone leaves again later: the house arms normally, no skip.
    house["people"]["roberto"] = _away("roberto", _LEFT)
    asyncio.run(PA.tick())
    assert house["confirm"] == ["arm"]
    assert house["notify"] == []


def test_someone_already_home_when_it_was_set_does_not_clear_it(house) -> None:
    """The usual case: set at home before leaving. Only a later arrival clears."""

    house["people"]["roberto"] = _home("roberto", _SET_AT - timedelta(hours=1))
    S.set_skip_away_arm_override(True, at=_SET_AT)

    asyncio.run(PA.tick())

    assert S.load_skip_away_arm_override() is True
    assert house["confirm"] == []


def test_set_then_everyone_leaves_skips_and_clears(house) -> None:
    house["people"]["roberto"] = _home("roberto", _SET_AT - timedelta(hours=1))
    S.set_skip_away_arm_override(True, at=_SET_AT)
    asyncio.run(PA.tick())
    assert S.load_skip_away_arm_override() is True

    house["people"]["roberto"] = _away("roberto", _LEFT)
    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert [e["outcome"] for e in house["trigger"]] == ["skipped"]
    assert S.load_skip_away_arm_override() is False


@pytest.mark.parametrize("path", ["skip", "arrival"])
def test_a_clear_that_cannot_be_saved_is_reported_not_confirmed(
    house, monkeypatch, path
) -> None:
    S.set_skip_away_arm_override(True, at=_SET_AT)
    if path == "arrival":
        house["people"]["roberto"] = _home("roberto", _SET_AT + timedelta(hours=1))

    def unsaveable(raw):
        raise OSError("disk full")

    monkeypatch.setattr(S, "_save_state", unsaveable)
    asyncio.run(PA.tick())

    status = S.load_skip_away_arm_status()
    assert status.active is True
    assert status.clear_failed_at is not None
    assert "disk full" in status.clear_error
    assert house["confirm"] == []

    # The store recovers: the next tick clears it and the note goes away.
    monkeypatch.setattr(S, "_save_state", P._save_state)
    asyncio.run(PA.tick())
    status = S.load_skip_away_arm_status()
    assert (status.active, status.clear_failed_at) == (False, None)


def test_every_set_is_logged(state, caplog) -> None:
    with caplog.at_level("INFO"):
        S.set_skip_away_arm_override(True, at=_SET_AT)
        S.set_skip_away_arm_override(False)
    messages = [r.getMessage() for r in caplog.records]
    assert any("switched on" in m and _SET_AT.isoformat() in m for m in messages)
    assert any("switched off" in m for m in messages)
