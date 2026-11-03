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
    P.set_skip_away_arm_override(True)
    people = [_away("roberto"), _away("ana")]
    decision = _decide(people, skip_away_arm=P.load_skip_away_arm_override())

    kids_home_was_on = P.record_away_arm_skip(decision.key)

    assert kids_home_was_on is True
    assert P.load_skip_away_arm_override() is False
    assert P.load_kids_home_override() is False
    meta = json.loads(state.read_text(encoding="utf-8"))["automation"]
    assert meta["last_arm_key"] == decision.key
    assert meta["last_arm_outcome"] == "skipped"
    # Same departure, a tick later: nothing left to arm or skip.
    later = _NOW + timedelta(seconds=10)
    assert _decide(people, at=later, skip_away_arm=P.load_skip_away_arm_override()) is None


def test_the_next_departure_after_a_skip_arms_normally(state) -> None:
    P.set_skip_away_arm_override(True)
    first = _decide([_away("roberto")], skip_away_arm=P.load_skip_away_arm_override())
    P.record_away_arm_skip(first.key)

    left_again = _LEFT + timedelta(hours=3)
    at = left_again + timedelta(minutes=6)
    people = [P.PersonPresence("roberto", "away", at, state_since=left_again)]
    decision = _decide(people, at=at, skip_away_arm=P.load_skip_away_arm_override())

    assert decision is not None
    assert (decision.kind, decision.action) == ("arm", "arm")


def test_override_persists_and_loads(state) -> None:
    assert P.load_skip_away_arm_override() is False
    P.set_skip_away_arm_override(True)
    assert P.load_skip_away_arm_override() is True
    P.set_skip_away_arm_override(False)
    assert P.load_skip_away_arm_override() is False


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
    return calls


def test_tick_skips_the_arm_clears_the_override_and_announces_it(house) -> None:
    P.set_skip_away_arm_override(True)

    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert house["follow"] == []
    assert P.load_skip_away_arm_override() is False
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
    P.set_skip_away_arm_override(True)

    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert P.load_skip_away_arm_override() is False
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
    P.set_skip_away_arm_override(True)

    def unsaveable(key):
        raise StoreUnreadableError("presence_state.json unreadable")

    monkeypatch.setattr(PA, "record_away_arm_skip", unsaveable)
    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert house["notify"] == []
    assert P.load_skip_away_arm_override() is True

    # The store recovers: the next tick records the skip, still no arm.
    monkeypatch.setattr(PA, "record_away_arm_skip", P.record_away_arm_skip)
    asyncio.run(PA.tick())

    assert house["confirm"] == []
    assert P.load_skip_away_arm_override() is False
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
            P.set_skip_away_arm_override(True)
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
    assert P.load_skip_away_arm_override() is False
