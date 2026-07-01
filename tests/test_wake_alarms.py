from __future__ import annotations

from datetime import datetime

from src.wake_alarms import (
    DAYS,
    WakeAlarmEntry,
    load_wake_alarms,
    set_wake_alarms,
    wake_alarm_due,
)
from src.wake_timers import cancel_timer, create_timer, list_timers, mark_expired


def test_wake_alarm_store_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "wake_alarms.json"

    entries = set_wake_alarms(
        [
            {
                "id": " weekday wakeup ",
                "label": "Wake up",
                "enabled": True,
                "time": "7am",
                "days": ["MON", "fri", "bad"],
            },
            {
                "id": "",
                "label": "",
                "enabled": False,
                "time": "05:30",
                "days": [],
                "date": "2026-08-12",
            },
        ],
        path=path,
    )

    assert entries[0].id == "weekday-wakeup"
    assert entries[0].time == "07:00"  # invalid time falls back to default
    assert entries[0].days == ["mon", "fri"]
    assert entries[0].date is None
    assert entries[1].id == "alarm-2"
    assert entries[1].enabled is False
    assert entries[1].date == "2026-08-12"
    assert load_wake_alarms(path=path) == entries


def test_wake_alarm_invalid_date_falls_back_to_recurring(tmp_path) -> None:
    path = tmp_path / "wake_alarms.json"
    entries = set_wake_alarms([{"id": "x", "time": "08:00", "date": "not-a-date"}], path=path)
    assert entries[0].date is None
    assert entries[0].days == list(DAYS)


def test_wake_alarm_due_respects_weekday_and_grace() -> None:
    entry = WakeAlarmEntry(id="wake", time="07:00", days=["mon", "tue"])

    assert wake_alarm_due(entry, datetime(2026, 6, 22, 7, 0, 30), 120) is True
    assert wake_alarm_due(entry, datetime(2026, 6, 22, 7, 3, 0), 120) is False
    assert wake_alarm_due(entry, datetime(2026, 6, 24, 7, 0, 30), 120) is False  # wrong weekday


def test_wake_alarm_due_one_shot_ignores_days() -> None:
    entry = WakeAlarmEntry(id="once", time="05:30", days=["mon"], date="2026-08-12")

    assert wake_alarm_due(entry, datetime(2026, 8, 12, 5, 30, 10), 120) is True
    assert wake_alarm_due(entry, datetime(2026, 8, 13, 5, 30, 10), 120) is False  # wrong date
    assert wake_alarm_due(entry, datetime(2026, 8, 5, 5, 30, 10), 120) is False  # not mon-only path


def test_wake_alarm_tick_fires_once_and_disables_one_shot(monkeypatch) -> None:
    import app.webapp.wake_alarm_automation as engine

    recurring = WakeAlarmEntry(id="daily", time="07:00", days=["mon"])
    one_shot = WakeAlarmEntry(id="once", time="07:00", days=["mon"], date="2026-06-22")
    saved: list = []
    notified: list = []

    monkeypatch.setattr(engine, "load_wake_alarms", lambda: [recurring, one_shot])
    monkeypatch.setattr(engine, "save_wake_alarms", lambda entries: saved.append(entries))
    monkeypatch.setattr(engine, "_notify", lambda message: notified.append(message))

    config = engine.WakeAlarmConfig(enabled=True, poll_interval_s=15)
    state = engine._EngineState(last_fire_day={})
    now = datetime(2026, 6, 22, 7, 0, 10)

    try:
        engine.tick(config, state, now)
        engine.tick(config, state, now)  # same tick again — must not re-fire either entry

        assert len(notified) == 2  # one per entry, only on the first tick
        assert state.last_fire_day == {"daily": "2026-06-22", "once": "2026-06-22"}
        assert {"daily", "once"} <= engine.ringing_alarm_ids()
        # Only the one-shot entry got persisted back disabled; recurring is untouched.
        assert len(saved) == 1
        saved_ids = {e.id: e.enabled for e in saved[0]}
        assert saved_ids["once"] is False
        assert saved_ids["daily"] is True
    finally:
        engine.dismiss_alarm("daily")
        engine.dismiss_alarm("once")


def test_dismiss_alarm_clears_ringing_state(monkeypatch) -> None:
    import app.webapp.wake_alarm_automation as engine

    engine._ringing_alarm_ids.add("some-alarm")
    assert engine.dismiss_alarm("some-alarm") is True
    assert "some-alarm" not in engine.ringing_alarm_ids()
    assert engine.dismiss_alarm("some-alarm") is False


def test_wake_timer_lifecycle() -> None:
    entry = create_timer("Pasta", 300)
    assert entry.seconds == 300
    assert [t.id for t in list_timers()] == [entry.id]

    # Not yet due.
    assert mark_expired(entry.ends_at - 10) == []
    # Due now — transitions once, not again on a repeat call.
    expired = mark_expired(entry.ends_at + 1)
    assert [e.id for e in expired] == [entry.id]
    assert mark_expired(entry.ends_at + 1) == []

    assert cancel_timer(entry.id) is True
    assert cancel_timer(entry.id) is False
    assert list_timers() == []


def test_wake_timer_seconds_are_clamped() -> None:
    entry = create_timer("too long", 999_999_999)
    try:
        assert entry.seconds == 24 * 60 * 60
    finally:
        cancel_timer(entry.id)
