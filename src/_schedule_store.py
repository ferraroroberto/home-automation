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
from pathlib import Path
from typing import Any, List

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SET = frozenset(DAYS)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def read_json(path: Path, default: Any) -> Any:
    """Return parsed JSON from ``path``, or ``default`` if absent/unreadable."""

    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return default


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
