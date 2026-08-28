"""Persisted reminder entries — bidirectional voice/app sync (issue #314).

Mirrors :mod:`src.wake_alarms`' architecture: the browser edits a single list
of entries, and the voice surface writes into the same list, so a reminder
created either way shows up everywhere. Unlike a wake alarm, a reminder's
payload is free-text *content* first and a due date/time second (often
absent entirely) — ``done`` marks it complete rather than firing/disabling it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import clean_date, read_json, safe_id, save_json
from src._spoken_time import extract_date, extract_time

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REMINDERS_PATH = _CONFIG_DIR / "reminders.json"

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass(frozen=True)
class ReminderEntry:
    """One free-text reminder, optionally due on a date/time.

    ``created_at`` (ISO timestamp) breaks ties between undated reminders when
    picking "the next one" for voice complete — oldest first (FIFO).
    """

    id: str
    text: str
    done: bool = False
    date: Optional[str] = None
    time: Optional[str] = None
    created_at: str = ""


def _clean_optional_time(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or not _TIME_RE.match(raw):
        return None
    hour, minute = (int(part) for part in raw.split(":", 1))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def clean_entry(raw: dict, fallback_id: str) -> ReminderEntry:
    """Coerce untrusted JSON/API data into a reminder entry."""

    date = clean_date(raw.get("date"))
    return ReminderEntry(
        id=safe_id(raw.get("id"), fallback_id),
        text=str(raw.get("text") or "").strip()[:200],
        done=bool(raw.get("done")),
        date=date,
        # A time only means anything alongside a date.
        time=_clean_optional_time(raw.get("time")) if date else None,
        created_at=str(raw.get("created_at") or "").strip(),
    )


def load_reminders(path: Optional[Path] = None) -> List[ReminderEntry]:
    """Return the persisted reminder list, or ``[]`` if absent."""

    target = Path(path) if path is not None else REMINDERS_PATH
    raw = read_json(target, [])
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_entry(item, f"reminder-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def save_reminders(entries: List[ReminderEntry], path: Optional[Path] = None) -> None:
    """Atomically persist the whole reminder list."""

    target = Path(path) if path is not None else REMINDERS_PATH
    save_json(target, [asdict(entry) for entry in entries])


def set_reminders(raw_entries: List[dict], path: Optional[Path] = None) -> List[ReminderEntry]:
    """Replace the reminder list with normalized entries and return it."""

    entries = [
        clean_entry(item, f"reminder-{idx}")
        for idx, item in enumerate(raw_entries, start=1)
        if isinstance(item, dict)
    ]
    save_reminders(entries, path)
    return entries


# --------------------------------------------------------------------------- #
# Next-occurrence helpers (voice "mark my reminder done" targets the soonest)
# --------------------------------------------------------------------------- #
def next_due(entry: ReminderEntry, now: datetime) -> datetime:
    """Sort key for "which reminder is next": its due date/time if set, else
    its creation time (so undated reminders fall back to FIFO order) — a due
    reminder always sorts ahead of an undated one created earlier."""

    if entry.date:
        hour, minute = (int(part) for part in (entry.time or "00:00").split(":", 1))
        return datetime.strptime(entry.date, "%Y-%m-%d").replace(hour=hour, minute=minute)
    try:
        return datetime.max - (now - datetime.fromisoformat(entry.created_at))
    except (ValueError, TypeError):
        return datetime.max


def soonest_pending(entries: List[ReminderEntry], now: datetime) -> Optional[ReminderEntry]:
    """The not-done entry due next, or ``None`` when none are pending."""

    pending = [entry for entry in entries if not entry.done]
    if not pending:
        return None
    return min(pending, key=lambda entry: next_due(entry, now))


def describe_reminder(entry: ReminderEntry) -> str:
    """A short, speakable description, e.g. ``"take out the trash, Thursday July 2 at 6 PM"``."""

    if not entry.date:
        return entry.text

    hour, minute = (int(part) for part in (entry.time or "00:00").split(":", 1))
    day = datetime.strptime(entry.date, "%Y-%m-%d")
    try:
        when = day.strftime("%A %B %-d")
    except ValueError:  # %-d is POSIX-only; Windows needs %#d
        when = day.strftime("%A %B %#d")
    if not entry.time:
        return f"{entry.text}, {when}"
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    clock = f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"
    return f"{entry.text}, {when} at {clock}"


# --------------------------------------------------------------------------- #
# Spoken-phrase parsing ("take out the trash at 6pm", "call mom tomorrow", …)
#
# The cue grammar itself lives in :mod:`src._spoken_time`, shared with
# ``calendar_events`` — only the reminder-specific shaping stays here.
# --------------------------------------------------------------------------- #
def parse_spoken_reminder(phrase: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Turn a spoken phrase into a raw reminder dict, or ``None`` if there's no text.

    Splits an optional trailing due-date/time cue (``at 6pm``, ``tomorrow``,
    ``today``, ``on friday``) off the phrase; whatever remains is the
    reminder's ``text``. A phrase with no recognisable cue is a plain,
    undated reminder — most "remind me to X" phrases have no time at all.

    Returns a dict shaped for :func:`clean_entry` (no ``id``/``created_at`` —
    the caller assigns those). English only — this repo's Spanish voice
    pipeline doesn't route reminders (no reminder.yaml under
    ``custom_sentences/es/``), unlike wake alarms.
    """

    text = " ".join(str(phrase or "").lower().split())
    if not text:
        return None

    rest, time_str = extract_time(text)
    rest, date_str = extract_date(rest, now)
    rest = " ".join(rest.split())
    if not rest:
        return None

    return {
        "text": rest,
        "done": False,
        "date": date_str,
        "time": time_str if date_str else None,
    }
