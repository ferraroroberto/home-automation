from __future__ import annotations

from datetime import datetime

from src.wake_alarms import (
    DAYS,
    WakeAlarmEntry,
    clean_entry,
    describe_alarm,
    load_wake_alarms,
    next_fire,
    parse_spoken_alarm,
    set_wake_alarms,
    soonest_enabled,
    wake_alarm_due,
)
from src.wake_timers import cancel_timer, create_timer, list_timers, mark_expired

# A fixed Wednesday 09:00 reference for the spoken-phrase / next-fire tests.
_NOW = datetime(2026, 7, 1, 9, 0, 0)


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


def test_wake_alarm_due_catches_late_night_window_after_midnight() -> None:
    entry = WakeAlarmEntry(id="bedtime", time="23:59", days=["fri"])

    assert wake_alarm_due(entry, datetime(2026, 7, 4, 0, 0, 30), 120) is True
    assert wake_alarm_due(entry, datetime(2026, 7, 4, 0, 1, 1), 120) is False


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


# ------------------------------------------------------ spoken-phrase parsing
def _parsed_entry(phrase: str) -> WakeAlarmEntry:
    raw = parse_spoken_alarm(phrase, _NOW)
    assert raw is not None, f"expected a parse for {phrase!r}"
    return clean_entry({**raw, "id": "x"}, "x")


def test_parse_spoken_alarm_time_forms() -> None:
    assert _parsed_entry("7 am").time == "07:00"
    assert _parsed_entry("7 pm").time == "19:00"
    assert _parsed_entry("7 30").time == "07:30"
    assert _parsed_entry("half past six").time == "06:30"
    assert _parsed_entry("quarter to seven").time == "06:45"
    assert _parsed_entry("quarter past 8").time == "08:15"
    assert _parsed_entry("seven thirty").time == "07:30"
    assert _parsed_entry("noon").time == "12:00"
    assert _parsed_entry("17 30").time == "17:30"


def test_parse_spoken_alarm_schedules() -> None:
    assert _parsed_entry("7 am on weekdays").days == ["mon", "tue", "wed", "thu", "fri"]
    assert _parsed_entry("7 on weekends").days == ["sat", "sun"]
    assert _parsed_entry("7 every day").days == list(DAYS)
    assert _parsed_entry("9 on monday").days == ["mon"]
    # unscheduled → every day
    assert _parsed_entry("7 am").days == list(DAYS)


def test_parse_spoken_alarm_one_shot_dates() -> None:
    tomorrow = _parsed_entry("8 tomorrow")
    assert tomorrow.date == "2026-07-02"
    today = _parsed_entry("8 today")
    assert today.date == "2026-07-01"


def test_parse_spoken_alarm_rejects_timeless_phrase() -> None:
    assert parse_spoken_alarm("banana", _NOW) is None
    assert parse_spoken_alarm("", _NOW) is None


def test_next_fire_recurring_and_one_shot() -> None:
    # Wednesday now; a Friday alarm fires this coming Friday.
    friday = WakeAlarmEntry(id="f", time="07:00", days=["fri"])
    assert next_fire(friday, _NOW) == datetime(2026, 7, 3, 7, 0)
    # Today at a later hour still counts as today.
    later = WakeAlarmEntry(id="l", time="10:00", days=["wed"])
    assert next_fire(later, _NOW) == datetime(2026, 7, 1, 10, 0)
    # A one-shot fires on its own date.
    once = WakeAlarmEntry(id="o", time="05:30", date="2026-08-12")
    assert next_fire(once, _NOW) == datetime(2026, 8, 12, 5, 30)


def test_soonest_enabled_picks_earliest_and_skips_disabled() -> None:
    entries = [
        WakeAlarmEntry(id="a", time="10:00", days=["wed"]),  # still upcoming today
        WakeAlarmEntry(id="b", time="06:00", days=["thu"]),  # tomorrow
        WakeAlarmEntry(id="c", enabled=False, time="05:00"),
    ]
    assert soonest_enabled(entries, _NOW).id == "a"  # today 10:00 beats tomorrow 06:00
    assert soonest_enabled([], _NOW) is None
    assert soonest_enabled([entries[2]], _NOW) is None  # all disabled


def test_soonest_enabled_ignores_past_one_shot() -> None:
    entries = [
        WakeAlarmEntry(id="expired", time="05:30", date="2026-06-30"),
        WakeAlarmEntry(id="recurring", time="10:00", days=["wed"]),
        WakeAlarmEntry(id="future", time="08:00", date="2026-07-02"),
    ]

    assert next_fire(entries[0], _NOW) == datetime.max
    assert soonest_enabled(entries, _NOW).id == "recurring"


def test_describe_alarm_phrasing() -> None:
    assert describe_alarm(WakeAlarmEntry(id="x", time="07:00")) == "7 AM every day"
    weekdays = WakeAlarmEntry(id="x", time="07:00", days=["mon", "tue", "wed", "thu", "fri"])
    assert describe_alarm(weekdays) == "7 AM on weekdays"
    assert describe_alarm(WakeAlarmEntry(id="x", time="18:30", days=["sat", "sun"])) == "6:30 PM on weekends"
    assert describe_alarm(WakeAlarmEntry(id="x", time="05:30", date="2026-08-12")) == "5:30 AM on Wednesday August 12"
