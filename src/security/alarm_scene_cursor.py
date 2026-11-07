"""Persisted cursor for the alarm-scene event-log scan.

``app/webapp/alarm_scene_automation.py`` detects a capture-worthy alarm onset
from RISCO's own event log (``src.security.risco_client.fetch_events``),
diffed against the last-processed alarm-event timestamp persisted here rather
than in memory. RISCO's system-wide ``ongoing_alarm`` / ``memory_alarm`` flags
latch ``True`` until a full disarm+dismiss cycle, so they cannot distinguish a
second real alarm on the same detector within one still-armed session from the
first; reading the event log instead gives each discrete "Alarm" event its own
capture regardless of that latch, and persisting the cursor to disk means a
process restart mid-session resumes from where it left off instead of
re-baselining. Same atomic load/save shape as ``alarm_scene_config`` /
``display_names``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src._atomic_json import write_json_atomic
from src.security.risco_client import EVENT_STAMP_CONVENTION, migrate_event_cursor

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CURSOR_PATH = _CONFIG_DIR / "alarm_scene_cursor.json"


def load_last_alarm_event_time(path: Optional[Path] = None) -> Optional[str]:
    """Return the ISO8601 UTC timestamp of the last-processed alarm event, or ``None``."""

    target = Path(path) if path is not None else CURSOR_PATH
    if not target.exists():
        return None
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); ignoring cursor", target, exc)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("last_alarm_event_time")
    return migrate_event_cursor(str(value) if value else None, data.get("stamp_convention"))


def save_last_alarm_event_time(value: str, path: Optional[Path] = None) -> None:
    """Atomically persist the last-processed alarm-event timestamp."""

    target = Path(path) if path is not None else CURSOR_PATH
    write_json_atomic(
        target,
        {"last_alarm_event_time": value, "stamp_convention": EVENT_STAMP_CONVENTION},
        indent=None,
        ensure_ascii=True,
    )
    logger.info("💾 Saved %s", target)
