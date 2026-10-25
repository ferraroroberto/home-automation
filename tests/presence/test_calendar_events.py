from __future__ import annotations

from datetime import datetime

from src.calendar_events import (
    DEFAULT_EVENT_DURATION_MINUTES,
    describe_calendar_event,
    parse_spoken_calendar_event,
    to_google_event_body,
)

# A fixed Wednesday 09:00 reference, same convention as tests/test_reminders.py.
_NOW = datetime(2026, 7, 1, 9, 0, 0)


def _parsed(phrase: str) -> dict:
    raw = parse_spoken_calendar_event(phrase, _NOW)
    assert raw is not None, f"expected a parse for {phrase!r}"
    return raw


# ------------------------------------------------------ spoken-phrase parsing
def test_parse_date_and_time_gives_a_timed_event() -> None:
    parsed = _parsed("dentist appointment to my calendar tomorrow at 3pm")
    assert parsed["summary"] == "dentist appointment"
    assert parsed["all_day"] is False
    assert parsed["date"] == "2026-07-02"
    assert parsed["time"] == "15:00"


def test_parse_date_only_gives_an_all_day_event() -> None:
    parsed = _parsed("team offsite to my calendar on friday")
    assert parsed["summary"] == "team offsite"
    assert parsed["all_day"] is True
    assert parsed["date"] == "2026-07-03"
    assert parsed["time"] is None


def test_parse_time_only_defaults_to_today_when_time_has_not_passed() -> None:
    # _NOW is 09:00; 3pm hasn't happened yet today.
    parsed = _parsed("dentist appointment at 3pm")
    assert parsed["date"] == "2026-07-01"
    assert parsed["time"] == "15:00"


def test_parse_time_only_defaults_to_tomorrow_when_time_has_passed() -> None:
    # _NOW is 09:00; 8am has already passed today.
    parsed = _parsed("dentist appointment at 8am")
    assert parsed["date"] == "2026-07-02"
    assert parsed["time"] == "08:00"


def test_parse_rejects_neither_date_nor_time() -> None:
    assert parse_spoken_calendar_event("dentist appointment", _NOW) is None


def test_parse_rejects_empty_phrase() -> None:
    assert parse_spoken_calendar_event("", _NOW) is None
    assert parse_spoken_calendar_event("   ", _NOW) is None


def test_parse_rejects_cue_only_phrase_with_no_summary() -> None:
    # Everything is consumed by the filler + date/time cues; nothing left.
    assert parse_spoken_calendar_event("to my calendar tomorrow at 3pm", _NOW) is None


def test_parse_strips_on_the_calendar_filler_too() -> None:
    parsed = _parsed("walk the dog on the calendar today")
    assert parsed["summary"] == "walk the dog"
    assert parsed["date"] == "2026-07-01"
    assert parsed["all_day"] is True


def test_parse_24h_time() -> None:
    parsed = _parsed("call the dentist at 14:30 tomorrow")
    assert parsed["time"] == "14:30"
    assert parsed["date"] == "2026-07-02"


# --------------------------------------------------------------- event shaping
def test_to_google_event_body_all_day_end_date_is_exclusive_next_day() -> None:
    body = to_google_event_body(
        {"summary": "team offsite", "all_day": True, "date": "2026-07-03", "time": None},
        "Europe/Madrid",
    )
    assert body["summary"] == "team offsite"
    assert body["start"] == {"date": "2026-07-03"}
    assert body["end"] == {"date": "2026-07-04"}


def test_to_google_event_body_timed_event_has_timezone_and_default_duration() -> None:
    body = to_google_event_body(
        {"summary": "dentist appointment", "all_day": False, "date": "2026-07-02", "time": "15:00"},
        "Europe/Madrid",
    )
    assert body["start"] == {"dateTime": "2026-07-02T15:00:00", "timeZone": "Europe/Madrid"}
    assert body["end"] == {"dateTime": "2026-07-02T16:00:00", "timeZone": "Europe/Madrid"}
    assert DEFAULT_EVENT_DURATION_MINUTES == 60


# --------------------------------------------------------------- descriptions
def test_describe_calendar_event_timed() -> None:
    parsed = {"summary": "dentist appointment", "all_day": False, "date": "2026-07-02", "time": "15:00"}
    assert describe_calendar_event(parsed) == "dentist appointment, Thursday July 2 at 3 PM"


def test_describe_calendar_event_all_day() -> None:
    parsed = {"summary": "team offsite", "all_day": True, "date": "2026-07-03", "time": None}
    assert describe_calendar_event(parsed) == "team offsite, Friday July 3, all day"
