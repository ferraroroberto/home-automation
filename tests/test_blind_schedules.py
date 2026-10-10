"""Blind schedules (issue #871): store, presence rule, due window and engine.

No real blind is ever touched: ``move_blinds`` / ``cover_device_ids`` /
``load_people`` are faked on the engine module and the clock is passed in.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.webapp import blind_schedules as engine
from src import blind_automation as B
from src._schedule_store import daily_due

# 2026-10-05 is a Monday, 2026-10-10 a Saturday.
MONDAY_0800 = datetime(2026, 10, 5, 8, 0, 30)
SATURDAY_0800 = datetime(2026, 10, 10, 8, 0, 30)


# ------------------------------------------------------------------- store
def test_clean_entry_coerces_untrusted_input() -> None:
    entry = B.clean_schedule_entry(
        {"id": "a b", "time": "7:5", "days": ["MON", "xyz", "mon"], "action": "explode",
         "targets": ["blind-1", "", "blind-1"], "presence": "maybe", "enabled": "yes"},
        "fallback",
    )
    assert entry.id == "a-b"
    assert entry.time == "08:00"  # "7:5" is not HH:MM
    assert entry.days == ["mon"]
    assert entry.action == "open"
    assert entry.targets == ["blind-1"]
    assert entry.presence == "any"
    assert entry.enabled is True


def test_store_round_trips_and_missing_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "blind_schedules.json"
    assert B.load_blind_schedules(path) == []
    saved = B.set_blind_schedules(
        [{"id": "down", "time": "21:30", "action": "close", "presence": "away",
          "days": ["sat", "sun"], "targets": ["blind-2"]}],
        path,
    )
    assert B.load_blind_schedules(path) == saved
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk[0]["action"] == "close" and on_disk[0]["targets"] == ["blind-2"]


def test_committed_sample_parses() -> None:
    sample = Path(__file__).resolve().parent.parent / "config" / "blind_schedules.sample.json"
    entries = B.load_blind_schedules(sample)
    assert entries and all(e.action in B.SCHEDULE_ACTIONS for e in entries)


# --------------------------------------------------------------- presence
@pytest.mark.parametrize(
    "condition, states, allowed",
    [
        ("any", [], True),
        ("home", ["home", "away"], True),
        ("home", ["away", "away"], False),
        ("away", ["away", "away"], True),
        ("away", ["home"], False),
        ("home", [], None),  # nobody tracked: unknown, never "nobody home"
        ("away", [], None),
    ],
)
def test_presence_allows(condition: str, states: list, allowed) -> None:
    assert B.presence_allows(condition, states)[0] is allowed


# -------------------------------------------------------------- due window
def test_daily_due_window_and_days() -> None:
    weekdays = ["mon", "tue", "wed", "thu", "fri"]
    assert daily_due("08:00", MONDAY_0800, 120, weekdays)
    assert not daily_due("08:00", SATURDAY_0800, 120, weekdays)
    assert daily_due("08:00", SATURDAY_0800, 120)  # None = every day
    assert not daily_due("08:00", MONDAY_0800.replace(minute=3), 120, weekdays)  # past grace
    assert not daily_due("08:01", MONDAY_0800, 120, weekdays)  # not yet
    assert not daily_due("nonsense", MONDAY_0800, 120)


# ------------------------------------------------------------------ engine
@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A fake house: two blinds, a presence roster, a schedule file, a recorder."""
    path = tmp_path / "blind_schedules.json"
    monkeypatch.setattr(B, "SCHEDULES_PATH", path)
    house = SimpleNamespace(people={"p1": "home"}, moves=[], path=path)

    async def _move(action, targets):
        house.moves.append((action, list(targets)))
        return [B.BlindOutcome(device_id=t, ok=True) for t in targets]

    monkeypatch.setattr(engine, "move_blinds", _move)
    monkeypatch.setattr(engine, "cover_device_ids", lambda: ["blind-1", "blind-2"])
    monkeypatch.setattr(
        engine, "load_people",
        lambda: {pid: SimpleNamespace(state=st) for pid, st in house.people.items()},
    )
    monkeypatch.setattr(engine, "_record", lambda *a, **k: None)
    return house


def _tick(state, now) -> None:
    asyncio.run(engine.tick(engine.BlindScheduleConfig(), state, now))


def test_due_entry_fires_once_per_day(world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}], world.path)
    state = engine._EngineState()
    _tick(state, MONDAY_0800)
    _tick(state, MONDAY_0800.replace(second=59))  # next poll, still in the window
    assert world.moves == [("open", ["blind-1", "blind-2"])]


def test_entry_does_not_fire_on_other_days_or_late(world) -> None:
    B.set_blind_schedules(
        [{"id": "up", "time": "08:00", "action": "open", "days": ["sat", "sun"]}], world.path
    )
    state = engine._EngineState()
    _tick(state, MONDAY_0800)
    _tick(state, SATURDAY_0800.replace(hour=14))  # a restart hours later never replays
    assert world.moves == []
    _tick(state, SATURDAY_0800)
    assert world.moves == [("open", ["blind-1", "blind-2"])]


def test_disabled_entry_never_fires(world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "enabled": False}], world.path)
    _tick(engine._EngineState(), MONDAY_0800)
    assert world.moves == []


def test_targets_are_filtered_to_known_blinds(world) -> None:
    B.set_blind_schedules(
        [{"id": "up", "time": "08:00", "targets": ["blind-2", "gone"]}], world.path
    )
    _tick(engine._EngineState(), MONDAY_0800)
    assert world.moves == [("open", ["blind-2"])]


def test_presence_miss_skips_for_the_day(world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "presence": "away"}], world.path)
    state = engine._EngineState()
    _tick(state, MONDAY_0800)  # someone is home: skipped
    world.people = {"p1": "away"}
    _tick(state, MONDAY_0800.replace(second=59))  # leaving later does not trigger it
    assert world.moves == []
    assert state.last_fire_day["up"] == "2026-10-05"


def test_unknown_presence_skips(world) -> None:
    world.people = {}
    B.set_blind_schedules([{"id": "up", "time": "08:00", "presence": "home"}], world.path)
    _tick(engine._EngineState(), MONDAY_0800)
    assert world.moves == []


def test_engine_respects_the_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "load_dotenv", lambda **kw: None)
    monkeypatch.setenv("BLIND_SCHEDULES_ENABLED", "0")
    assert engine.start_blind_schedules() is None
