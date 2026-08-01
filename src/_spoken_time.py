"""Single home for spoken date/time cue parsing (issue #571).

``src/reminders.py`` and ``src/calendar_events.py`` both turn a spoken phrase
("take out the trash at 6pm", "dentist tomorrow at 3pm") into a text remainder
plus optional ``HH:MM`` / ``YYYY-MM-DD`` cues. The two used to carry verbatim
copies of ``_fmt_time`` / ``_extract_time`` / ``_extract_date`` and the weekday
vocabulary; they now import from here so the cue vocabulary can never drift
between the reminder and calendar voice surfaces.

Pure parsing only — no I/O, no persistence. What differs between the two
callers (a reminder may be undated, a calendar event may not) stays in the
caller.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

WEEKDAY_WORDS = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}


def fmt_time(hour: int, minute: int) -> Optional[str]:
    """Format an in-range ``(hour, minute)`` pair as ``HH:MM``, else ``None``."""

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def extract_time(text: str) -> Tuple[str, Optional[str]]:
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
        return rest, fmt_time(hour, minute)

    m = re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, fmt_time(int(m.group(1)), int(m.group(2)))

    return text, None


def extract_date(text: str, now: datetime) -> Tuple[str, Optional[str]]:
    """Strip a trailing day/date cue, returning ``(rest, date)``."""

    m = re.search(r"\btomorrow\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, (now + timedelta(days=1)).strftime("%Y-%m-%d")

    m = re.search(r"\btoday\b", text)
    if m:
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        return rest, now.strftime("%Y-%m-%d")

    m = re.search(r"\bon\s+(" + "|".join(WEEKDAY_WORDS) + r")\b", text)
    if m:
        target = WEEKDAY_WORDS[m.group(1)]
        rest = (text[: m.start()] + " " + text[m.end():]).strip()
        for offset in range(1, 8):
            cand = now + timedelta(days=offset)
            if cand.strftime("%a").lower()[:3] == target:
                return rest, cand.strftime("%Y-%m-%d")

    return text, None
