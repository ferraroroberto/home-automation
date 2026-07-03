"""Persisted weekly RISCO alarm schedules.

The browser edits a single list of entries. The webapp-owned background task
loads that same list and applies due entries through ``src.risco_client``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from src.risco_client import ACTIONS

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SCHEDULES_PATH = _CONFIG_DIR / "security_schedules.json"

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SET = frozenset(DAYS)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass(frozen=True)
class SecurityScheduleEntry:
    """One weekly alarm schedule entry."""

    id: str
    enabled: bool = True
    time: str = "21:00"
    days: List[str] | None = None
    action: str = "arm"

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
    raw = str(value or "21:00").strip()
    if not _TIME_RE.match(raw):
        return "21:00"
    hour, minute = (int(part) for part in raw.split(":", 1))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return "21:00"


def _clean_days(value: Any) -> List[str]:
    if not isinstance(value, list):
        return list(DAYS)
    seen: List[str] = []
    for item in value:
        day = str(item).strip().lower()[:3]
        if day in _DAY_SET and day not in seen:
            seen.append(day)
    return seen or list(DAYS)


def _clean_action(value: Any) -> str:
    action = str(value or "arm").strip().lower()
    return action if action in ACTIONS else "arm"


def clean_entry(raw: dict, fallback_id: str) -> SecurityScheduleEntry:
    """Coerce untrusted JSON/API data into a schedule entry."""

    return SecurityScheduleEntry(
        id=_safe_id(raw.get("id"), fallback_id),
        enabled=raw.get("enabled") is not False,
        time=_clean_time(raw.get("time")),
        days=_clean_days(raw.get("days")),
        action=_clean_action(raw.get("action")),
    )


def load_security_schedules(path: Optional[Path] = None) -> List[SecurityScheduleEntry]:
    """Return the persisted alarm schedule list, or ``[]`` if absent."""

    target = Path(path) if path is not None else SCHEDULES_PATH
    raw = _read_json(target)
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_entry(item, f"schedule-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def save_security_schedules(
    entries: List[SecurityScheduleEntry],
    path: Optional[Path] = None,
) -> None:
    """Atomically persist the whole alarm schedule list."""

    target = Path(path) if path is not None else SCHEDULES_PATH
    _save(target, [asdict(entry) for entry in entries])


def set_security_schedules(raw_entries: List[dict], path: Optional[Path] = None) -> List[SecurityScheduleEntry]:
    """Replace the schedule list with normalized entries and return it."""

    entries = [
        clean_entry(item, f"schedule-{idx}")
        for idx, item in enumerate(raw_entries, start=1)
        if isinstance(item, dict)
    ]
    save_security_schedules(entries, path)
    return entries


def schedule_due(entry: SecurityScheduleEntry, now: datetime, grace_s: int) -> bool:
    """True when ``now`` is inside this entry's local fire window."""

    hour, minute = (int(part) for part in entry.time.split(":", 1))
    days = set(entry.days or [])
    for schedule_day in (now, now - timedelta(days=1)):
        if schedule_day.strftime("%a").lower()[:3] not in days:
            continue
        fire_at = schedule_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now - fire_at).total_seconds()
        if 0 <= delta < grace_s:
            return True
    return False
