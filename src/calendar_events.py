"""Spoken-phrase parsing + Google Calendar event shaping (issue #313).

Google Calendar is the only store — this repo persists nothing locally, so
there's no dataclass/atomic-store pair here the way ``wake_alarms.py`` and
``reminders.py`` have one. This module is pure parsing/shaping (no Google
imports, no I/O); ``src/calendar_write.py`` is the only place that talks to
the Google Calendar API.

Adapted from (not copied from) ``src/reminders.py``'s ``_extract_time``/
``_extract_date`` — same due-cue vocabulary (``at 6pm``, ``tomorrow``,
``on friday``) — but a calendar event, unlike a reminder, needs *some* date
to be meaningful: unlike ``parse_spoken_reminder``, a phrase with neither an
explicit nor an inferable date returns ``None`` rather than falling back to
an undated entry.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

DEFAULT_EVENT_DURATION_MINUTES = 60

_WEEKDAY_WORDS = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}

# Strips filler embedded mid-phrase ("dentist appointment TO MY CALENDAR
# tomorrow at 3pm") — a single trailing wildcard can't carve this out as a
# separate hassil slot when a date/time cue trails it, so Python does it.
_CALENDAR_FILLER_RE = re.compile(r"\b(?:to|on|into|in)\s+(?:my|the)\s+calendar\b")


def _fmt_time(hour: int, minute: int) -> Optional[str]:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _extract_time(text: str) -> Tuple[str, Optional[str]]:
    """Strip a trailing ``at <time>`` fragment, returning ``(rest, time)``."""

    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3) == "p" and hour < 12:
            hour += 12
        elif m.group(3) == "a" and hour == 12:
            hour = 0
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, _fmt_time(hour, minute)

    m = re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, _fmt_time(int(m.group(1)), int(m.group(2)))

    return text, None


def _extract_date(text: str, now: datetime) -> Tuple[str, Optional[str]]:
    """Strip a trailing day/date cue, returning ``(rest, date)``."""

    m = re.search(r"\btomorrow\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, (now + timedelta(days=1)).strftime("%Y-%m-%d")

    m = re.search(r"\btoday\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, now.strftime("%Y-%m-%d")

    m = re.search(r"\bon\s+(" + "|".join(_WEEKDAY_WORDS) + r")\b", text)
    if m:
        target = _WEEKDAY_WORDS[m.group(1)]
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        for offset in range(1, 8):
            cand = now + timedelta(days=offset)
            if cand.strftime("%a").lower()[:3] == target:
                return rest, cand.strftime("%Y-%m-%d")

    return text, None


def parse_spoken_calendar_event(phrase: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Turn a spoken phrase into a raw calendar-event dict, or ``None``.

    Splits an optional trailing time cue (``at 6pm``, ``at 14:30``) and an
    optional trailing date cue (``tomorrow``, ``today``, ``on friday``) off
    the phrase, then strips embedded calendar filler ("to my calendar", "on
    the calendar") from what's left — the remainder is the event summary.

    A time with no date defaults to **today** if that time hasn't passed yet,
    else **tomorrow**. Neither a date nor a time cue, or no summary text left
    after stripping, returns ``None`` — a calendar event needs a date to be
    meaningful, unlike a reminder.
    """

    text = " ".join(str(phrase or "").lower().split())
    if not text:
        return None

    rest, time_str = _extract_time(text)
    rest, date_str = _extract_date(rest, now)
    rest = _CALENDAR_FILLER_RE.sub(" ", rest)
    summary = " ".join(rest.split())

    if date_str is None:
        if time_str is None:
            return None
        hour, minute = (int(part) for part in time_str.split(":", 1))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        date_str = (now if candidate >= now else now + timedelta(days=1)).strftime("%Y-%m-%d")

    if not summary:
        return None

    return {
        "summary": summary,
        "all_day": time_str is None,
        "date": date_str,
        "time": time_str,
    }


def to_google_event_body(parsed: Dict[str, Any], tz_name: str) -> Dict[str, Any]:
    """Shape a parsed dict into a Google Calendar API v3 event body.

    An all-day event's ``end.date`` is Google's **exclusive** end — a
    one-day all-day event's end must be the *next* calendar date, or Google
    silently creates a zero-day event. A timed event passes an offset-less
    ``dateTime`` alongside an explicit IANA ``timeZone`` so Google resolves
    DST correctly.
    """

    body: Dict[str, Any] = {"summary": parsed["summary"]}
    if parsed["all_day"]:
        start_date = datetime.strptime(parsed["date"], "%Y-%m-%d")
        end_date = start_date + timedelta(days=1)
        body["start"] = {"date": parsed["date"]}
        body["end"] = {"date": end_date.strftime("%Y-%m-%d")}
        return body

    hour, minute = (int(part) for part in parsed["time"].split(":", 1))
    start_dt = datetime.strptime(parsed["date"], "%Y-%m-%d").replace(hour=hour, minute=minute)
    end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)
    body["start"] = {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name}
    body["end"] = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name}
    return body


def describe_calendar_event(parsed: Dict[str, Any]) -> str:
    """A short, speakable description, e.g. ``"dentist appointment, Thursday July 2 at 3 PM"``."""

    day = datetime.strptime(parsed["date"], "%Y-%m-%d")
    try:
        when = day.strftime("%A %B %-d")
    except ValueError:  # %-d is POSIX-only; Windows needs %#d
        when = day.strftime("%A %B %#d")

    if parsed["all_day"]:
        return f"{parsed['summary']}, {when}, all day"

    hour, minute = (int(part) for part in parsed["time"].split(":", 1))
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    clock = f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"
    return f"{parsed['summary']}, {when} at {clock}"
