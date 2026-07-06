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


def test_security_schedule_due_respects_weekday_and_grace() -> None:
    entry = SecurityScheduleEntry(
        id="night",
        enabled=True,
        time="21:00",
        days=["mon", "tue"],
        action="arm",
    )

    assert schedule_due(entry, datetime(2026, 6, 22, 21, 0, 30), 120) is True
    assert schedule_due(entry, datetime(2026, 6, 22, 21, 3, 0), 120) is False
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

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    # Prevent real Telegram sends and real log writes during this unit test.
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))

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

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "arm", "error")]
    assert recorded[0]["dedupe_key"] == "schedule:arm-fails"


def test_security_schedule_tick_confirms_disarm_success(monkeypatch) -> None:
    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="disarm-ok", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_confirm(action: str) -> object:
        return _FakeState("disarmed")

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "confirm_alarm_action", fake_confirm)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {"disarm-ok": "2026-06-22"}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "ok")]
