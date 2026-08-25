"""Shared read/clean helpers for weekly schedule-entry JSON stores (issue #327).

The ``_read_json`` / ``_save`` / ``_safe_id`` / ``_clean_time`` / ``_clean_days``
helpers below were duplicated verbatim across :mod:`src.wake_alarms` and
:mod:`src.security_schedules` (the read/save pair a third time in
:mod:`src.hvac_automation`, per its README note that ``wake_alarms`` was
"cloned from security_schedules.py's atomic store"). Centralized here;
callers keep their own dataclass shape and entry-cleaning logic — only the
read/save mechanics and the id/time/days field cleaners are shared.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, List

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SET = frozenset(DAYS)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")

# A store that exists but momentarily can't be opened is nearly always another
# process's `os.replace` window — on Windows that surfaces as a sharing
# violation (`PermissionError`/`[Errno 13]`) lasting milliseconds. Retry a few
# times before concluding anything; the total stall is bounded well under a
# second so it's safe on the webapp's event loop, where every background tick
# reads these stores.
_READ_ATTEMPTS = 4
_READ_RETRY_DELAY_S = 0.05


class StoreUnreadableError(RuntimeError):
    """A store file exists but its contents could not be established.

    Deliberately *not* interchangeable with "the file isn't there" (issue
    #689). Every :func:`read_json` caller in this repo is a read-modify-save
    store, so handing back an empty default on a failed read means the next
    save persists that phantom empty over the real data. That is exactly how a
    transient sharing violation on ``config/presence_state.json`` erased the
    presence roster and let auto-arm fire "everyone away" on a household of
    two with one person asleep inside.

    Raising instead keeps the failure in its own state — unknown, not empty —
    so a background loop logs a failed tick and simply doesn't act, and nothing
    is written on top of data nobody could read.
    """


def read_json(path: Path, default: Any) -> Any:
    """Return parsed JSON from ``path``, or ``default`` if the file is absent.

    Raises :class:`StoreUnreadableError` when the file *exists* but cannot be
    read or parsed after :data:`_READ_ATTEMPTS` tries — an absent store and an
    unreadable one are different facts and must not share a return value.
    """

    last_exc: Exception | None = None
    for attempt in range(_READ_ATTEMPTS):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_exc = exc
        if attempt + 1 < _READ_ATTEMPTS:
            time.sleep(_READ_RETRY_DELAY_S)
    # Re-check rather than assume: a store legitimately deleted mid-retry is
    # absent, not unreadable, and still deserves the default.
    if not path.exists():
        return default
    logger.error(
        "❌ Could not read %s after %d attempts (%s) — refusing to treat it as empty",
        path,
        _READ_ATTEMPTS,
        last_exc,
    )
    raise StoreUnreadableError(f"could not read {path}: {last_exc}") from last_exc


def save_json(path: Path, data: Any) -> None:
    """Atomically persist ``data`` to ``path`` and log the standard save message."""

    write_json_atomic(path, data)
    logger.info("💾 Saved %s", path)


def safe_id(value: Any, fallback: str) -> str:
    """Sanitize an untrusted id into a DOM/key-safe slug, or ``fallback``."""

    raw = str(value or fallback).strip()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return safe or fallback


def clean_time(value: Any, default: str) -> str:
    """Coerce an untrusted value into a valid ``HH:MM`` string, or ``default``."""

    raw = str(value or default).strip()
    if not _TIME_RE.match(raw):
        return default
    hour, minute = (int(part) for part in raw.split(":", 1))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return default


def clean_days(value: Any) -> List[str]:
    """Coerce an untrusted value into a de-duplicated list of valid day codes."""

    if not isinstance(value, list):
        return list(DAYS)
    seen: List[str] = []
    for item in value:
        day = str(item).strip().lower()[:3]
        if day in _DAY_SET and day not in seen:
            seen.append(day)
    return seen or list(DAYS)
