from __future__ import annotations

import asyncio
from datetime import datetime

from src.security_schedules import (
    DAYS,
    SecurityScheduleEntry,
    load_security_schedules,
    schedule_due,
    set_security_schedules,
)


def test_security_schedule_store_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "security_schedules.json"

    entries = set_security_schedules(
        [
            {
                "id": " weeknight full ",
                "enabled": True,
                "time": "9pm",
                "days": ["MON", "fri", "bad"],
                "action": "perimeter",
            },
            {
                "id": "",
                "enabled": False,
                "time": "07:15",
                "days": [],
                "action": "unknown",
            },
        ],
        path=path,
    )

    assert entries[0].id == "weeknight-full"
    assert entries[0].time == "21:00"
    assert entries[0].days == ["mon", "fri"]
    assert entries[0].action == "perimeter"
    assert entries[1].id == "schedule-2"
    assert entries[1].enabled is False
    assert entries[1].days == list(DAYS)
    assert entries[1].action == "arm"
    assert load_security_schedules(path=path) == entries


def test_security_schedule_due_respects_weekday_and_stays_due_all_day() -> None:
    entry = SecurityScheduleEntry(
        id="night",
        enabled=True,
        time="21:00",
        days=["mon", "tue"],
        action="arm",
    )

    assert schedule_due(entry, datetime(2026, 6, 22, 21, 0, 30), 120) is True
    # #527: still due hours later the same day — a schedule that first fails
    # (e.g. a transient RISCO outage) keeps getting retried, not just given a
    # narrow window and then abandoned until the same time tomorrow. tick()'s
    # own last_fire_day bookkeeping (tested separately) is what actually stops
    # a schedule firing twice once it succeeds.
    assert schedule_due(entry, datetime(2026, 6, 22, 21, 3, 0), 120) is True
    assert schedule_due(entry, datetime(2026, 6, 22, 23, 59, 0), 120) is True
    # Not yet due before the fire time.
    assert schedule_due(entry, datetime(2026, 6, 22, 20, 59, 59), 120) is False
    # Wrong weekday (Wednesday); Tuesday's slot from the day before is also
    # long past its short backward-look grace window by this point.
    assert schedule_due(entry, datetime(2026, 6, 24, 21, 0, 30), 120) is False


def test_security_schedule_due_catches_late_night_window_after_midnight() -> None:
    entry = SecurityScheduleEntry(
        id="bedtime",
        enabled=True,
        time="23:59",
        days=["fri"],
        action="arm",
    )

    assert schedule_due(entry, datetime(2026, 7, 4, 0, 0, 30), 120) is True
    assert schedule_due(entry, datetime(2026, 7, 4, 0, 1, 1), 120) is False


class _FakeState:
    """Minimal stand-in for ``SecurityState`` - only ``mode`` is read by
    ``action_took_effect``."""

    def __init__(self, mode: str) -> None:
        self.mode = mode


def test_security_schedule_tick_fires_once_and_logs_failures(monkeypatch) -> None:
    """The retry/backoff mechanics of confirming an action now live in
    ``confirm_alarm_action`` (shared with presence) and are covered in
    ``tests/test_alarm_notify.py``. Here ``confirm_alarm_action`` is faked
    directly - this test is about tick()'s own day-based fire/retry bookkeeping."""

    import app.webapp.security_automation as engine

    calls: list[str] = []
    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="ok", time="21:00", days=["mon"], action="arm"),
        SecurityScheduleEntry(id="bad", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_confirm(action: str) -> object:
        calls.append(action)
        if action == "disarm":
            raise engine.RiscoCommandError("panel down")
        return _FakeState("armed")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    # Prevent real Telegram sends and real log writes during this unit test.
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))
    asyncio.run(engine.tick(config, state, now))

    assert calls == ["arm", "disarm", "disarm"]
    assert state.last_fire_day == {"ok": "2026-06-22"}
    # Both schedule entries record their outcome; "bad" fires twice (tick retries failed entries).
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert ("schedule", "arm", "ok") in outcomes
    assert outcomes.count(("schedule", "disarm", "error")) == 2


def test_security_schedule_tick_alerts_after_confirm_exhausts_retries(monkeypatch) -> None:
    """Once ``confirm_alarm_action`` gives up (a persistent mismatch, retried
    and still unconfirmed), ``_apply_schedule`` alerts and must not mark the
    schedule as fired so tick() retries it on its own next poll."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="arm-fails", time="21:00", days=["mon"], action="arm"),
    ]

    async def fake_confirm(action: str) -> object:
        raise engine.RiscoCommandError(f"panel read back 'disarmed' after {action}, not the expected state")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "arm", "error")]
    assert recorded[0]["dedupe_key"] == "schedule:arm-fails"


def test_security_schedule_tick_alerts_on_missing_credentials(monkeypatch) -> None:
    """Issue #610: a RiscoConfigError (missing RISCO_USERNAME/PASSWORD/PIN)
    must alert + log like any other automatic-action failure, and must not
    mark the schedule as fired so tick() retries it on its own next poll."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="no-creds", time="05:00", days=["mon"], action="disarm"),
    ]

    async def fake_confirm(action: str) -> object:
        raise engine.RiscoConfigError(
            "Missing credentials. Copy .env.example to .env and set "
            "RISCO_USERNAME, RISCO_PASSWORD and RISCO_PIN "
            "(your RISCO Cloud login and panel PIN)."
        )

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 5, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "error")]
    assert "Missing credentials" in recorded[0]["error"]
    assert recorded[0]["dedupe_key"] == "schedule:no-creds"


def test_security_schedule_tick_retries_failed_entry_hours_later_same_day(monkeypatch) -> None:
    """#527: a schedule that fails right after its fire time must not be
    abandoned until tomorrow — it should still fire successfully hours later
    the same day, once whatever was wrong (a transient RISCO outage) clears."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="flaky", time="05:00", days=["mon"], action="disarm"),
    ]
    outcomes = iter(["fail", "ok"])

    async def fake_confirm(action: str) -> object:
        if next(outcomes) == "fail":
            raise engine.RiscoCommandError("panel read back 'perimeter' after disarm, not the expected state")
        return _FakeState("disarmed")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})

    # First attempt, right at the fire time, fails.
    asyncio.run(engine.tick(config, state, datetime(2026, 6, 22, 5, 4, 0)))
    assert state.last_fire_day == {}

    # Hours later, well past the old ~120s grace window, it succeeds.
    asyncio.run(engine.tick(config, state, datetime(2026, 6, 22, 11, 0, 0)))
    assert state.last_fire_day == {"flaky": "2026-06-22"}

    outcomes_seen = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes_seen == [
        ("schedule", "disarm", "error"),
        ("schedule", "disarm", "ok"),
    ]

    # A third tick the same day must not re-apply the now-succeeded schedule.
    asyncio.run(engine.tick(config, state, datetime(2026, 6, 22, 15, 0, 0)))
    assert len(recorded) == 2


def test_security_schedule_last_fire_day_survives_restart(monkeypatch) -> None:
    """#540: a restart after a schedule's fire time must not refire it the
    same day. ``schedule_due()`` deliberately stays true for the rest of the
    day (#527, for same-process retry after a failed confirm), so the guard
    against a *second* fire has to survive the process going away - a fresh
    ``_EngineState`` reloaded from disk, simulating a tray/webapp restart,
    must recall that today's fire already happened."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="perimeter-21", time="21:00", days=["mon"], action="perimeter"),
    ]

    async def fake_confirm(action: str) -> object:
        return _FakeState("perimeter")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    now = datetime(2026, 6, 22, 21, 0, 10)

    # First process: the schedule fires and its fire state is persisted.
    state = engine._EngineState(last_fire_day=engine._load_last_fire_day())
    asyncio.run(engine.tick(config, state, now))
    assert state.last_fire_day == {"perimeter-21": "2026-06-22"}
    assert len(recorded) == 1

    # Restart: a brand-new process reloads state from disk instead of
    # starting with an empty dict, then polls again later the same day.
    restarted_state = engine._EngineState(last_fire_day=engine._load_last_fire_day())
    assert restarted_state.last_fire_day == {"perimeter-21": "2026-06-22"}

    later_same_day = datetime(2026, 6, 22, 21, 6, 40)
    asyncio.run(engine.tick(config, restarted_state, later_same_day))

    # Still due per schedule_due() (#527), but must not have fired again.
    assert len(recorded) == 1


def test_security_schedule_tick_confirms_disarm_success(monkeypatch) -> None:
    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="disarm-ok", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_confirm(action: str) -> object:
        return _FakeState("disarmed")

    async def fake_record_alarm_action(**kw) -> None:
        recorded.append(kw)

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", fake_record_alarm_action)

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {"disarm-ok": "2026-06-22"}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "ok")]
