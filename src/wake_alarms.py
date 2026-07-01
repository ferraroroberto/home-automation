"""Persisted wake-alarm entries (issue #304).

The browser edits a single list of entries. The webapp-owned background task
(``app.webapp.wake_alarm_automation``) loads that same list, fires due
entries, and rearms (weekly) or auto-disables (one-shot) them afterward.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
WAKE_ALARMS_PATH = _CONFIG_DIR / "wake_alarms.json"

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SET = frozenset(DAYS)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class WakeAlarmEntry:
    """One wake-alarm entry — recurring (``days``) or one-shot (``date``).

    ``date`` takes precedence when set: the alarm fires once on that local
    date, then the caller (the background loop) disables it. Otherwise it
    recurs weekly on ``days`` (defaults to every day when empty).
    """

    id: str
    label: str = ""
    enabled: bool = True
    time: str = "07:00"
    days: List[str] | None = None
    date: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "days", list(self.days or DAYS))


def _read_json(path: Path) -> Any:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return []


def _save(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("💾 Saved %s", path)


def _safe_id(value: Any, fallback: str) -> str:
    raw = str(value or fallback).strip()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return safe or fallback


def _clean_time(value: Any) -> str:
    raw = str(value or "07:00").strip()
    if not _TIME_RE.match(raw):
        return "07:00"
    hour, minute = (int(part) for part in raw.split(":", 1))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return "07:00"


def _clean_date(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or not _DATE_RE.match(raw):
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def _clean_days(value: Any) -> List[str]:
    if not isinstance(value, list):
        return list(DAYS)
    seen: List[str] = []
    for item in value:
        day = str(item).strip().lower()[:3]
        if day in _DAY_SET and day not in seen:
            seen.append(day)
    return seen or list(DAYS)


def clean_entry(raw: dict, fallback_id: str) -> WakeAlarmEntry:
    """Coerce untrusted JSON/API data into a wake-alarm entry."""

    return WakeAlarmEntry(
        id=_safe_id(raw.get("id"), fallback_id),
        label=str(raw.get("label") or "").strip()[:80],
        enabled=raw.get("enabled") is not False,
        time=_clean_time(raw.get("time")),
        days=_clean_days(raw.get("days")),
        date=_clean_date(raw.get("date")),
    )


def load_wake_alarms(path: Optional[Path] = None) -> List[WakeAlarmEntry]:
    """Return the persisted wake-alarm list, or ``[]`` if absent."""

    target = Path(path) if path is not None else WAKE_ALARMS_PATH
    raw = _read_json(target)
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_entry(item, f"alarm-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def save_wake_alarms(entries: List[WakeAlarmEntry], path: Optional[Path] = None) -> None:
    """Atomically persist the whole wake-alarm list."""

    target = Path(path) if path is not None else WAKE_ALARMS_PATH
    _save(target, [asdict(entry) for entry in entries])


def set_wake_alarms(raw_entries: List[dict], path: Optional[Path] = None) -> List[WakeAlarmEntry]:
    """Replace the wake-alarm list with normalized entries and return it."""

    entries = [
        clean_entry(item, f"alarm-{idx}")
        for idx, item in enumerate(raw_entries, start=1)
        if isinstance(item, dict)
    ]
    save_wake_alarms(entries, path)
    return entries


def wake_alarm_due(entry: WakeAlarmEntry, now: datetime, grace_s: int) -> bool:
    """True when ``now`` is inside this entry's local fire window."""

    if entry.date:
        if now.strftime("%Y-%m-%d") != entry.date:
            return False
    elif now.strftime("%a").lower()[:3] not in set(entry.days or []):
        return False
    hour, minute = (int(part) for part in entry.time.split(":", 1))
    fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (now - fire_at).total_seconds()
    return 0 <= delta < grace_s
