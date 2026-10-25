"""Persisted per-zone "auto-bypass after repeated alarms" overrides (issue #341).

RISCO's own panel has an undocumented, uncontrolled anti-nuisance auto-omit —
confirmed live in issue #325 (a perimeter zone auto-bypassed itself
after 5 alarms in one armed session). This store lets the user configure a
much tighter, per-zone threshold (1-3 repeats) so a windy garden or a roaming
cat gets bypassed for the rest of the current armed session well before the
panel's own opaque limit, instead of re-triggering the scene-capture/notify
pipeline over and over. ``app/webapp/security_override_automation.py`` loads
this list and does the counting/bypassing; it never bypasses a zone with no
enabled entry here.

Stored at gitignored ``config/security_override.json`` (the repo is public —
zone ids are home-identifying); the committed ``…sample.json`` is the
template. Atomic temp-file + ``os.replace`` write, the same load/save shape as
``alarm_scene_config`` / ``security_schedules``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from src._schedule_store import (
    clean_entry_list,
    load_entry_list,
    safe_id,
    save_entry_list,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
OVERRIDES_PATH = _CONFIG_DIR / "security_override.json"

# 1-3 is the only sane range the user asked for — a single retry disables a
# detector on its very first (possibly genuine) alarm, and beyond 3 defeats
# the point of getting ahead of the panel's own ~5-alarm auto-omit.
MIN_MAX_RETRIES = 1
MAX_MAX_RETRIES = 3


@dataclass(frozen=True)
class OverrideEntry:
    """One detector's "bypass after N repeats this session" rule."""

    id: str
    zone_id: int
    max_retries: int
    enabled: bool = True


def _clamp_retries(value: Any) -> Optional[int]:
    try:
        retries = int(value)
    except (TypeError, ValueError):
        return None
    return max(MIN_MAX_RETRIES, min(MAX_MAX_RETRIES, retries))


def clean_override(raw: dict, fallback_id: str) -> Optional[OverrideEntry]:
    """Coerce untrusted JSON/API data into an entry, or ``None`` if unusable.

    An entry without a numeric ``zone_id`` or a valid ``max_retries`` is
    dropped — those two fields are the whole point of the rule.
    """

    try:
        zone_id = int(raw.get("zone_id"))
    except (TypeError, ValueError):
        return None
    retries = _clamp_retries(raw.get("max_retries"))
    if retries is None:
        return None
    return OverrideEntry(
        id=safe_id(raw.get("id"), fallback_id),
        zone_id=zone_id,
        max_retries=retries,
        enabled=raw.get("enabled") is not False,
    )


def load_overrides(path: Optional[Path] = None) -> List[OverrideEntry]:
    """Return the persisted override list, or ``[]`` if absent/unreadable."""

    target = Path(path) if path is not None else OVERRIDES_PATH
    # Silent on a non-list file, unlike the schedule stores (kept as before).
    return load_entry_list(target, clean_override, "override", warn_not_list=False)


def save_overrides(entries: List[OverrideEntry], path: Optional[Path] = None) -> None:
    """Atomically persist the whole override list."""

    target = Path(path) if path is not None else OVERRIDES_PATH
    save_entry_list(target, entries)


def set_overrides(
    raw_entries: List[dict], path: Optional[Path] = None
) -> List[OverrideEntry]:
    """Replace the override list with normalized entries and return it."""

    entries = clean_entry_list(raw_entries, clean_override, "override")
    save_overrides(entries, path)
    return entries
