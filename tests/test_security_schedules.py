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
        return _FakeState("armed")

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "control_system", fake_control)
    # Prevent real Telegram sends and real log writes during this unit test.
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))
    # No inner backoff retries here - this test is about tick()'s own outer
    # retry (calling tick() again on the next poll), not _apply_schedule's.
    monkeypatch.setattr(engine, "_RETRY_DELAYS_S", ())

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


def test_security_schedule_tick_alerts_on_confirmed_arm_mismatch(monkeypatch) -> None:
    """The WebUI call doesn't raise, but the re-read state stays disarmed (e.g.
    a door/window was open) - this must alert exactly like a raised exception,
    and must not mark the schedule as fired so tick() retries it."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="arm-fails", time="21:00", days=["mon"], action="arm"),
    ]

    async def fake_control(action: str) -> object:
        return _FakeState("disarmed")

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "control_system", fake_control)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(engine, "_RETRY_DELAYS_S", ())

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "arm", "error")]


def test_security_schedule_tick_alerts_on_confirmed_disarm_mismatch(monkeypatch) -> None:
    """Same as above for disarm - the panel stays armed after a disarm command."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="disarm-fails", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_control(action: str) -> object:
        return _FakeState("armed")

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "control_system", fake_control)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(engine, "_RETRY_DELAYS_S", ())

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "error")]


def test_security_schedule_tick_confirms_disarm_success(monkeypatch) -> None:
    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entries = [
        SecurityScheduleEntry(id="disarm-ok", time="21:00", days=["mon"], action="disarm"),
    ]

    async def fake_control(action: str) -> object:
        return _FakeState("disarmed")

    monkeypatch.setattr(engine, "load_security_schedules", lambda: entries)
    monkeypatch.setattr(engine, "control_system", fake_control)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))

    config = engine.SecurityScheduleConfig(enabled=True, poll_interval_s=60)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 21, 0, 10)

    asyncio.run(engine.tick(config, state, now))

    assert state.last_fire_day == {"disarm-ok": "2026-06-22"}
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "ok")]


def test_apply_schedule_retries_transient_mismatch_and_does_not_alert(monkeypatch) -> None:
    """A read-back mismatch that clears on retry (issue #388's 05:00 false
    alarm - a transient RISCO cloud lag) must not alert at all."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entry = SecurityScheduleEntry(id="disarm-lag", time="05:00", days=["mon"], action="disarm")
    calls: list[str] = []

    async def fake_control(action: str) -> object:
        calls.append(action)
        # First two attempts still read back the pre-disarm state; the third
        # (after the cloud lag clears) confirms disarmed.
        return _FakeState("perimeter" if len(calls) < 3 else "disarmed")

    monkeypatch.setattr(engine, "control_system", fake_control)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(engine, "note_manual_alarm_action", lambda action: None)
    monkeypatch.setattr(engine, "_RETRY_DELAYS_S", (0, 0, 0))

    asyncio.run(engine._apply_schedule(entry))

    assert calls == ["disarm", "disarm", "disarm"]
    outcomes = [(r["source"], r["action"], r["outcome"]) for r in recorded]
    assert outcomes == [("schedule", "disarm", "ok")]


def test_apply_schedule_alerts_once_after_exhausting_all_retries(monkeypatch) -> None:
    """A genuine persistent failure - mismatched on every attempt, including a
    raised RiscoCommandError - still alerts, exactly once, after the last retry."""

    import app.webapp.security_automation as engine

    recorded: list[dict] = []
    entry = SecurityScheduleEntry(id="arm-stuck", time="21:00", days=["mon"], action="arm")
    calls: list[str] = []

    async def fake_control(action: str) -> object:
        calls.append(action)
        if len(calls) == 2:
            raise engine.RiscoCommandError("panel unreachable")
        return _FakeState("disarmed")

    monkeypatch.setattr(engine, "control_system", fake_control)
    monkeypatch.setattr(engine, "record_alarm_action", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(engine, "_RETRY_DELAYS_S", (0, 0, 0))

    try:
        asyncio.run(engine._apply_schedule(entry))
        raised = False
    except engine.RiscoCommandError:
        raised = True

    assert raised is True
    # Initial attempt + all three retries = 4 calls, one alert at the end.
    assert calls == ["arm", "arm", "arm", "arm"]
    assert len(recorded) == 1
    assert recorded[0]["source"] == "schedule"
    assert recorded[0]["action"] == "arm"
    assert recorded[0]["outcome"] == "error"
    assert recorded[0]["dedupe_key"] == "schedule:arm-stuck"
