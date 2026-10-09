"""Blinds following the automatic alarm (issue #875).

The decision, the daytime rule and :func:`follow_alarm` are exercised with a
fake ``move_blinds``; no real blind, schedule file or location file is read.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import blind_schedules as engine
from src import blind_automation as B

# 2026-10-05 is a Monday.
MONDAY = datetime(2026, 10, 5)


def _entry(time: str, action: str, days=None, enabled: bool = True) -> B.BlindScheduleEntry:
    return B.BlindScheduleEntry(id=f"{action}-{time}", time=time, action=action,
                                days=days, enabled=enabled)


# ---------------------------------------------------------------- decision
@pytest.mark.parametrize(
    "kind, action, enabled, daytime, expected",
    [
        ("arm", "arm", True, None, "close"),        # everyone left, full arm
        ("arm", "perimeter", True, None, None),     # kids-home override: blinds stay up
        ("arm", "partial", True, None, None),
        ("disarm", "disarm", True, True, "open"),   # first arrival in daytime
        ("disarm", "disarm", True, False, None),    # arrival at night
        ("disarm", "disarm", True, None, None),     # daytime unknown: leave them
        ("arm", "arm", False, None, None),          # pairing off
        ("disarm", "disarm", False, True, None),
        ("guardian_hold", "hold", True, True, None),
    ],
)
def test_alarm_blind_action(kind, action, enabled, daytime, expected) -> None:
    assert B.alarm_blind_action(kind, action, enabled=enabled, daytime=daytime)[0] == expected


# ---------------------------------------------------------------- daytime
def test_schedule_day_window_is_earliest_up_to_latest_down() -> None:
    entries = [
        _entry("08:30", "open", ["mon"]),
        _entry("07:00", "open", ["sat", "sun"]),     # another weekday
        _entry("10:00", "open", ["mon"]),
        _entry("21:30", "close", ["mon"]),
        _entry("19:00", "close", ["mon"]),
        _entry("06:00", "open", ["mon"], enabled=False),
    ]
    assert B.schedule_day_window(entries, "mon") == ("08:30", "21:30")
    assert B.schedule_day_window(entries, "sat") is None  # no Down on Saturday
    assert B.schedule_day_window([_entry("22:00", "open"), _entry("07:00", "close")], "mon") is None


def test_is_daytime_uses_the_schedule_then_the_sun() -> None:
    entries = [_entry("08:00", "open"), _entry("21:00", "close")]
    assert B.is_daytime(MONDAY.replace(hour=12), entries, -30.0)[0] is True  # schedule wins
    assert B.is_daytime(MONDAY.replace(hour=23), entries, 30.0)[0] is False
    assert B.is_daytime(MONDAY.replace(hour=12), [], 12.0)[0] is True        # sun fallback
    assert B.is_daytime(MONDAY.replace(hour=23), [], -10.0)[0] is False
    assert B.is_daytime(MONDAY.replace(hour=12), [], None)[0] is None        # unknown


def test_prefs_default_off_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "blind_alarm.json"
    assert B.load_blind_alarm_prefs(path).follow_alarm is False
    B.save_blind_alarm_prefs(B.BlindAlarmPrefs(follow_alarm=True), path)
    assert B.load_blind_alarm_prefs(path).follow_alarm is True


def test_committed_sample_is_off() -> None:
    sample = Path(__file__).resolve().parent.parent / "config" / "blind_alarm.sample.json"
    assert B.load_blind_alarm_prefs(sample).follow_alarm is False


# ------------------------------------------------------------ follow_alarm
@pytest.fixture
def house(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(enabled=True, moves=[], schedule=[], sun=20.0, boom=False)

    async def _move(action, device_ids=None):
        if state.boom:
            raise RuntimeError("LAN on fire")
        state.moves.append(action)
        return [B.BlindOutcome(device_id="blind-1", ok=True)]

    monkeypatch.setattr(engine, "move_blinds", _move)
    monkeypatch.setattr(engine, "load_blind_alarm_prefs",
                        lambda: B.BlindAlarmPrefs(follow_alarm=state.enabled))
    monkeypatch.setattr(engine, "load_blind_schedules", lambda: state.schedule)
    monkeypatch.setattr(engine, "_sun_elevation_now", lambda: state.sun)
    monkeypatch.setattr(engine, "_record_event", lambda *a, **k: None)
    return state


def _follow(kind: str, action: str, hour: int = 12):
    return asyncio.run(engine.follow_alarm(kind, action, now=MONDAY.replace(hour=hour)))


def test_full_arm_lowers_every_blind(house) -> None:
    assert _follow("arm", "arm") == "close"
    assert house.moves == ["close"]


def test_kids_home_perimeter_arm_leaves_blinds_up(house) -> None:
    assert _follow("arm", "perimeter") is None
    assert house.moves == []


def test_daytime_disarm_raises_and_night_disarm_does_not(house) -> None:
    house.schedule = [_entry("08:00", "open"), _entry("21:00", "close")]
    assert _follow("disarm", "disarm", hour=12) == "open"
    assert _follow("disarm", "disarm", hour=23) is None
    assert house.moves == ["open"]


def test_pairing_off_moves_nothing(house) -> None:
    house.enabled = False
    assert _follow("arm", "arm") is None
    assert _follow("disarm", "disarm") is None
    assert house.moves == []


def test_a_blind_failure_never_raises_into_the_alarm_path(house) -> None:
    house.boom = True
    assert _follow("arm", "arm") is None  # swallowed and logged, not raised
