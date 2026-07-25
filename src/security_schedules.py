"""Persisted weekly RISCO alarm schedules.

The browser edits a single list of entries. The webapp-owned background task
loads that same list and applies due entries through ``src.risco_client``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from src._schedule_store import clean_days, clean_time, read_json, safe_id, save_json
from src.risco_client import ACTIONS

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SCHEDULES_PATH = _CONFIG_DIR / "security_schedules.json"

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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


def _clean_action(value: Any) -> str:
    action = str(value or "arm").strip().lower()
    return action if action in ACTIONS else "arm"


def clean_entry(raw: dict, fallback_id: str) -> SecurityScheduleEntry:
    """Coerce untrusted JSON/API data into a schedule entry."""

    return SecurityScheduleEntry(
        id=safe_id(raw.get("id"), fallback_id),
        enabled=raw.get("enabled") is not False,
        time=clean_time(raw.get("time"), "21:00"),
        days=clean_days(raw.get("days")),
        action=_clean_action(raw.get("action")),
    )


def load_security_schedules(path: Optional[Path] = None) -> List[SecurityScheduleEntry]:
    """Return the persisted alarm schedule list, or ``[]`` if absent."""

    target = Path(path) if path is not None else SCHEDULES_PATH
    raw = read_json(target, [])
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
    save_json(target, [asdict(entry) for entry in entries])


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
    """True when ``now`` is inside this entry's fire window.

    A schedule becomes due at its fire time and stays due for the rest of
    that same calendar day, so a schedule that first fails (a transient RISCO
    outage, say) keeps getting retried on later polls instead of only getting
    one narrow window and then waiting until the same time tomorrow (#527).
    ``tick()``'s own ``last_fire_day`` bookkeeping is what actually stops a
    schedule firing twice in one day, so widening this window is safe on its
    own — it only controls how long a *not-yet-successful* schedule keeps
    being retried.

    ``grace_s`` still bounds the one backward-looking case: a schedule whose
    fire time was very late the previous night, caught in the few minutes
    just after local midnight. That look-back is deliberately kept short —
    letting a stale "yesterday" identity retry for hours into today would let
    it consume today's own once-a-day fire slot.
    """

    hour, minute = (int(part) for part in entry.time.split(":", 1))
    days = set(entry.days or [])

    if now.strftime("%a").lower()[:3] in days:
        fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if fire_at <= now:
            return True

    yesterday = now - timedelta(days=1)
    if yesterday.strftime("%a").lower()[:3] in days:
        fire_at = yesterday.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now - fire_at).total_seconds()
        if 0 <= delta < grace_s:
            return True

    return False
