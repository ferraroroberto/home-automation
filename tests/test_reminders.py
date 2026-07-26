from __future__ import annotations

from datetime import datetime

from src.reminders import (
    ReminderEntry,
    clean_entry,
    describe_reminder,
    load_reminders,
    next_due,
    parse_spoken_reminder,
    set_reminders,
    soonest_pending,
)

# A fixed Wednesday 09:00 reference for the spoken-phrase / next-due tests.
_NOW = datetime(2026, 7, 1, 9, 0, 0)


def test_reminder_store_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "reminders.json"

    entries = set_reminders(
        [
            {
                "id": " take out the trash ",
                "text": "take out the trash",
                "done": False,
                "date": "2026-07-02",
                "time": "18:00",
                "created_at": "2026-07-01T09:00:00",
            },
            {
                "id": "",
                "text": "call mom",
                "done": True,
                "date": "",
                "time": "",
                "created_at": "2026-07-01T08:00:00",
            },
        ],
        path=path,
    )

    assert entries[0].id == "take-out-the-trash"
    assert entries[0].date == "2026-07-02"
    assert entries[0].time == "18:00"
    assert entries[1].id == "reminder-2"
    assert entries[1].done is True
    assert entries[1].date is None
    assert load_reminders(path=path) == entries


def test_reminder_time_without_date_is_dropped() -> None:
    entry = clean_entry({"id": "x", "text": "foo", "time": "18:00"}, "x")
    assert entry.date is None
    assert entry.time is None  # a time only means anything alongside a date


def test_reminder_invalid_date_is_dropped() -> None:
    entry = clean_entry({"id": "x", "text": "foo", "date": "not-a-date", "time": "18:00"}, "x")
    assert entry.date is None
    assert entry.time is None


def test_reminder_text_is_truncated() -> None:
    entry = clean_entry({"id": "x", "text": "a" * 500}, "x")
    assert len(entry.text) == 200


# ------------------------------------------------------ spoken-phrase parsing
def _parsed(phrase: str) -> dict:
    raw = parse_spoken_reminder(phrase, _NOW)
    assert raw is not None, f"expected a parse for {phrase!r}"
    return raw


def test_parse_spoken_reminder_plain_text_no_due() -> None:
    parsed = _parsed("call mom")
    assert parsed["text"] == "call mom"
    assert parsed["date"] is None
    assert parsed["time"] is None


def test_parse_spoken_reminder_extracts_trailing_time() -> None:
    parsed = _parsed("take out the trash at 6pm tomorrow")
    assert parsed["text"] == "take out the trash"
    assert parsed["date"] == "2026-07-02"
    assert parsed["time"] == "18:00"


def test_parse_spoken_reminder_today() -> None:
    parsed = _parsed("buy milk today")
    assert parsed["text"] == "buy milk"
    assert parsed["date"] == "2026-07-01"


def test_parse_spoken_reminder_weekday() -> None:
    # Wednesday now; "on friday" resolves to the coming Friday.
    parsed = _parsed("water the plants on friday")
    assert parsed["text"] == "water the plants"
    assert parsed["date"] == "2026-07-03"


def test_parse_spoken_reminder_24h_time() -> None:
    parsed = _parsed("call the dentist at 14:30 tomorrow")
    assert parsed["time"] == "14:30"
    assert parsed["date"] == "2026-07-02"


def test_parse_spoken_reminder_rejects_empty_or_time_only_phrase() -> None:
    assert parse_spoken_reminder("", _NOW) is None
    assert parse_spoken_reminder("   ", _NOW) is None
    assert parse_spoken_reminder("at 6pm tomorrow", _NOW) is None  # no reminder text left


# --------------------------------------------------------------- descriptions
def test_describe_reminder_undated_is_just_the_text() -> None:
    entry = ReminderEntry(id="x", text="call mom")
    assert describe_reminder(entry) == "call mom"


def test_describe_reminder_with_date_and_time() -> None:
    entry = ReminderEntry(id="x", text="take out the trash", date="2026-07-02", time="18:00")
    assert describe_reminder(entry) == "take out the trash, Thursday July 2 at 6 PM"


def test_describe_reminder_date_only() -> None:
    entry = ReminderEntry(id="x", text="water the plants", date="2026-07-03")
    assert describe_reminder(entry) == "water the plants, Friday July 3"


# ----------------------------------------------------------- soonest_pending
def test_soonest_pending_prefers_due_over_undated() -> None:
    due = ReminderEntry(id="due", text="due", date="2026-07-05", time="09:00", created_at=_NOW.isoformat())
    undated = ReminderEntry(id="undated", text="undated", created_at="2026-01-01T00:00:00")
    assert soonest_pending([undated, due], _NOW).id == "due"


def test_soonest_pending_undated_falls_back_to_fifo() -> None:
    older = ReminderEntry(id="older", text="older", created_at="2026-06-30T08:00:00")
    newer = ReminderEntry(id="newer", text="newer", created_at="2026-07-01T08:00:00")
    assert soonest_pending([newer, older], _NOW).id == "older"


def test_soonest_pending_skips_done_and_handles_empty() -> None:
    done = ReminderEntry(id="done", text="done", done=True, created_at="2026-01-01T00:00:00")
    assert soonest_pending([done], _NOW) is None
    assert soonest_pending([], _NOW) is None


def test_next_due_handles_missing_created_at() -> None:
    entry = ReminderEntry(id="x", text="x", created_at="")
    assert next_due(entry, _NOW) == datetime.max
