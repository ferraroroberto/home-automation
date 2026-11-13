"""The once-a-day schedule fire latch, pinned per engine.

Four engines fire a schedule entry at most once per local day: HVAC
schedules, blind schedules, alarm schedules and wake alarms. Each table below
pins one engine's behaviour at its own apply boundary (the device, panel or
notify call is always faked), so the same tables hold whichever latch
mechanism sits underneath:

* the due window opens at the entry's time and closes when it should;
* a failed fire is retried inside the window (HVAC, blinds, alarm) or
  consumed (wake);
* a deliberate skip consumes the day (blinds);
* a restart inside the window never fires the entry a second time.

``_fresh_state`` stands in for a new process: it builds the engine's state the
way ``_run`` does at boot, with nothing carried over in memory.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.webapp.automation as hvac
import app.webapp.blind_schedules as blinds
import app.webapp.security_automation as alarm
import app.webapp.wake_alarm_automation as wake
from src.devices import blind_automation as B
from src.devices.hvac_verify import VerifyConfig
from src.security.security_schedules import SecurityScheduleEntry
from src.wake_alarms import WakeAlarmEntry

# 2026-06-22 and 2026-10-05 are Mondays.
MON = datetime(2026, 6, 22, 8, 0, 10)


def _fresh_state(engine: Any) -> Any:
    """A brand-new engine state, as a just-restarted process would build it."""
    required = {
        f.name: {}
        for f in dataclasses.fields(engine._EngineState)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    return engine._EngineState(**required)


# --------------------------------------------------------------------- HVAC
class _Clock(datetime):
    current = MON

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


@pytest.fixture
def hvac_world(monkeypatch: pytest.MonkeyPatch):
    world = SimpleNamespace(applied=[], fail=False)
    unit = SimpleNamespace(unit_id="u1", name="Unit", power=False, operation_mode=None)
    sched = SimpleNamespace(id="s1", time="08:00", enabled=True)

    async def _devices():
        return [unit]

    async def _apply(_unit, entry):
        world.applied.append(entry.id)
        if world.fail:
            raise RuntimeError("unit offline")

    async def _no_boost(*_a, **_k):
        return None

    monkeypatch.setattr(hvac, "datetime", _Clock)
    monkeypatch.setattr(hvac, "load_rules", lambda: {})
    monkeypatch.setattr(hvac, "load_schedules", lambda: {"u1": [sched]})
    monkeypatch.setattr(hvac, "fetch_devices", _devices)
    monkeypatch.setattr(hvac, "_apply_schedule", _apply)
    monkeypatch.setattr(hvac, "_coordinate_boost", _no_boost)
    return world


def _hvac_tick(state: Any, now: datetime) -> None:
    _Clock.current = now
    # Verification is its own suite (tests/devices/test_hvac_verify.py); off here.
    config = hvac.AutomationConfig(verify=VerifyConfig(enabled=False))
    asyncio.run(hvac._tick(config, state))


def test_hvac_window_opens_at_time_and_closes_after_grace(hvac_world) -> None:
    state = _fresh_state(hvac)
    _hvac_tick(state, MON.replace(hour=7, minute=59))
    assert hvac_world.applied == []
    _hvac_tick(state, MON)
    _hvac_tick(state, MON.replace(second=50))
    assert hvac_world.applied == ["s1"]

    late = _fresh_state(hvac)
    _hvac_tick(late, MON.replace(minute=3))  # past the two-minute grace
    assert hvac_world.applied == ["s1"]


def test_hvac_failed_apply_retries_inside_the_window(hvac_world) -> None:
    state = _fresh_state(hvac)
    hvac_world.fail = True
    _hvac_tick(state, MON)
    hvac_world.fail = False
    _hvac_tick(state, MON.replace(minute=1))
    _hvac_tick(state, MON.replace(minute=1, second=30))
    assert hvac_world.applied == ["s1", "s1"]


def test_hvac_restart_inside_the_window_does_not_refire(hvac_world) -> None:
    _hvac_tick(_fresh_state(hvac), MON)
    _hvac_tick(_fresh_state(hvac), MON.replace(minute=1))
    assert hvac_world.applied == ["s1"]


def test_hvac_missed_window_logs_once(hvac_world, caplog: pytest.LogCaptureFixture) -> None:
    state = _fresh_state(hvac)
    hvac_world.fail = True
    with caplog.at_level(logging.INFO):
        _hvac_tick(state, MON)
        _hvac_tick(state, MON.replace(minute=5))
        _hvac_tick(state, MON.replace(minute=6))
    missed = [r for r in caplog.records if "missed" in r.getMessage()]
    assert len(missed) == 1
    assert "u1:s1" in missed[0].getMessage()


# ------------------------------------------------------------------- blinds
BLIND_MON = datetime(2026, 10, 5, 8, 0, 30)


@pytest.fixture
def blind_world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "blind_schedules.json"
    monkeypatch.setattr(B, "SCHEDULES_PATH", path)
    world = SimpleNamespace(people={"p1": "home"}, moves=[], fail=False, path=path)

    async def _move(action, targets):
        world.moves.append(action)
        if world.fail:
            raise FileNotFoundError("devices.json")
        return [B.BlindOutcome(device_id=t, ok=True) for t in targets]

    monkeypatch.setattr(blinds, "move_blinds", _move)
    monkeypatch.setattr(blinds, "cover_device_ids", lambda: ["blind-1"])
    monkeypatch.setattr(
        blinds, "load_people",
        lambda: {pid: SimpleNamespace(state=st) for pid, st in world.people.items()},
    )
    monkeypatch.setattr(blinds, "_record", lambda *a, **k: None)
    return world


def _blind_tick(state: Any, now: datetime) -> None:
    asyncio.run(blinds.tick(blinds.BlindScheduleConfig(), state, now))


def test_blinds_window_opens_at_time_and_closes_after_grace(blind_world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}], blind_world.path)
    state = _fresh_state(blinds)
    _blind_tick(state, BLIND_MON.replace(hour=7, minute=59))
    assert blind_world.moves == []
    _blind_tick(state, BLIND_MON)
    _blind_tick(state, BLIND_MON.replace(second=59))
    assert blind_world.moves == ["open"]

    _blind_tick(_fresh_state(blinds), BLIND_MON.replace(minute=3))
    assert blind_world.moves == ["open"]


def test_blinds_failed_move_retries_inside_the_window(blind_world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}], blind_world.path)
    state = _fresh_state(blinds)
    blind_world.fail = True
    _blind_tick(state, BLIND_MON)
    blind_world.fail = False
    _blind_tick(state, BLIND_MON.replace(minute=1))
    _blind_tick(state, BLIND_MON.replace(minute=1, second=30))
    assert blind_world.moves == ["open", "open"]


def test_blinds_presence_skip_consumes_the_day(blind_world) -> None:
    B.set_blind_schedules(
        [{"id": "up", "time": "08:00", "action": "open", "presence": "away"}], blind_world.path
    )
    state = _fresh_state(blinds)
    _blind_tick(state, BLIND_MON)  # someone is home: skipped
    blind_world.people = {"p1": "away"}
    _blind_tick(state, BLIND_MON.replace(minute=1))
    assert blind_world.moves == []


def test_blinds_restart_inside_the_window_does_not_refire(blind_world) -> None:
    B.set_blind_schedules([{"id": "up", "time": "08:00", "action": "open"}], blind_world.path)
    _blind_tick(_fresh_state(blinds), BLIND_MON)
    _blind_tick(_fresh_state(blinds), BLIND_MON.replace(minute=1))
    assert blind_world.moves == ["open"]


# -------------------------------------------------------------------- alarm
@pytest.fixture
def alarm_world(monkeypatch: pytest.MonkeyPatch):
    world = SimpleNamespace(applied=[], fail=False)
    entry = SecurityScheduleEntry(id="night", time="08:00", days=["mon"], action="arm")

    async def _apply(e):
        world.applied.append(e.id)
        if world.fail:
            raise alarm.RiscoCommandError("panel down")

    monkeypatch.setattr(alarm, "load_security_schedules", lambda: [entry])
    monkeypatch.setattr(alarm, "_apply_schedule", _apply)
    return world


def _alarm_tick(state: Any, now: datetime) -> None:
    asyncio.run(alarm.tick(alarm.SecurityScheduleConfig(), state, now))


def test_alarm_window_opens_at_time_and_stays_open_all_day(alarm_world) -> None:
    state = _fresh_state(alarm)
    _alarm_tick(state, MON.replace(hour=7, minute=59))
    assert alarm_world.applied == []
    _alarm_tick(state, MON)
    _alarm_tick(state, MON.replace(hour=20))
    assert alarm_world.applied == ["night"]


def test_alarm_failed_apply_retries_for_the_rest_of_the_day(alarm_world) -> None:
    state = _fresh_state(alarm)
    alarm_world.fail = True
    _alarm_tick(state, MON)
    alarm_world.fail = False
    _alarm_tick(state, MON.replace(hour=15))
    _alarm_tick(state, MON.replace(hour=16))
    assert alarm_world.applied == ["night", "night"]


def test_alarm_restart_later_the_same_day_does_not_refire(alarm_world) -> None:
    _alarm_tick(_fresh_state(alarm), MON)
    _alarm_tick(_fresh_state(alarm), MON.replace(hour=12))
    assert alarm_world.applied == ["night"]


def test_alarm_reads_a_latch_file_in_the_old_format(alarm_world, tmp_path) -> None:
    """A ``security_schedule_last_fire.json`` written before the shared latch
    (a flat ``{id: "YYYY-MM-DD"}`` object under ``logs/``) is still honoured."""
    path = tmp_path / "logs" / "security_schedule_last_fire.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"night": "2026-06-22"}), encoding="utf-8")
    _alarm_tick(_fresh_state(alarm), MON)
    assert alarm_world.applied == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"night": "2026-06-22"}


# --------------------------------------------------------------------- wake
@pytest.fixture
def wake_world(monkeypatch: pytest.MonkeyPatch):
    world = SimpleNamespace(fired=[], fail=False)
    entry = WakeAlarmEntry(id="daily", time="08:00", days=["mon"])

    async def _fire(e):
        world.fired.append(e.id)
        if world.fail:
            raise RuntimeError("notify down")

    monkeypatch.setattr(wake, "load_wake_alarms", lambda: [entry])
    monkeypatch.setattr(wake, "save_wake_alarms", lambda _entries: None)
    monkeypatch.setattr(wake, "_fire_alarm", _fire)
    monkeypatch.setattr(wake, "mark_expired", lambda _ts: [])
    return world


def _wake_tick(state: Any, now: datetime) -> None:
    asyncio.run(wake.tick(wake.WakeAlarmConfig(), state, now))


def test_wake_window_opens_at_time_and_closes_after_grace(wake_world) -> None:
    state = _fresh_state(wake)
    _wake_tick(state, MON.replace(hour=7, minute=59))
    assert wake_world.fired == []
    _wake_tick(state, MON)
    _wake_tick(state, MON.replace(second=40))
    assert wake_world.fired == ["daily"]

    _wake_tick(_fresh_state(wake), MON.replace(minute=2))  # past the 60 s grace
    assert wake_world.fired == ["daily"]


def test_wake_failed_fire_consumes_the_day(wake_world) -> None:
    state = _fresh_state(wake)
    wake_world.fail = True
    _wake_tick(state, MON)
    wake_world.fail = False
    _wake_tick(state, MON.replace(second=40))
    assert wake_world.fired == ["daily"]


def test_wake_restart_inside_the_window_does_not_refire(wake_world) -> None:
    _wake_tick(_fresh_state(wake), MON)
    _wake_tick(_fresh_state(wake), MON.replace(second=40))
    assert wake_world.fired == ["daily"]


# -------------------------------------------------------------- latch itself
def test_latch_writes_compact_json_and_never_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The latch file is compact UTF-8 JSON with no leftover ``.tmp``, and a
    filesystem error is logged rather than raised, so a failed persist never
    takes an engine down."""
    from src import activity_log
    from src._schedule_store import DailyFireLatch

    latch = DailyFireLatch(alarm.FIRE_LATCH_POLICY)
    latch.mark("ü", "2026-06-22")
    path = tmp_path / "logs" / "security_schedule_last_fire.json"
    assert path.read_text(encoding="utf-8") == '{"ü": "2026-06-22"}'
    assert list(path.parent.glob("*.tmp")) == []

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(activity_log, "LOGS_DIR", blocker)
    with caplog.at_level(logging.WARNING):
        latch.mark("k", "2026-06-22")
    assert any("Could not persist" in r.getMessage() for r in caplog.records)
    assert latch.fired("k", "2026-06-22")


def test_each_engine_declares_its_policy() -> None:
    """The per-engine differences live in one declared policy each."""
    table = {
        engine.FIRE_LATCH_POLICY.file_name: (
            engine.FIRE_LATCH_POLICY.window,
            engine.FIRE_LATCH_POLICY.mark_before_fire,
            engine.FIRE_LATCH_POLICY.skip_consumes_day,
        )
        for engine in (hvac, blinds, alarm, wake)
    }
    assert table == {
        "hvac_schedule_last_fire.json": ("grace", False, False),
        "blind_schedule_last_fire.json": ("grace", False, True),
        "security_schedule_last_fire.json": ("rest_of_day", False, False),
        "wake_alarm_last_fire.json": ("grace", True, False),
    }
