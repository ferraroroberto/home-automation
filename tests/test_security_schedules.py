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


def test_security_schedule_tick_fires_once_and_logs_failures(monkeypatch) -> None:
    import app.webapp.security_automation as engine

    calls: list[str] = []
    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="ok", time="21:00", days=["mon"], action="arm"),
        SecurityScheduleEntry(id="bad", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_control(action: str) -> object:
        calls.append(action)
        if action == "disarm":
            raise RuntimeError("panel down")
        return object()

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "control_system", fake_control)
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
