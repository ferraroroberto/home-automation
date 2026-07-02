"""Persisted cursor for the alarm-scene event-log scan (issue #325).

``app/webapp/alarm_scene_automation.py`` used to detect a capture-worthy alarm
onset from a false->true transition of RISCO's system-wide ``ongoing_alarm`` /
``memory_alarm`` flags, tracked in a process-lifetime in-memory dict. That flag
latches ``True`` until a full disarm+dismiss cycle, so a second real alarm on
the same detector within one still-armed session never produced a second
transition - and a webapp restart between the last dismiss and the first alarm
of a new session could swallow that first alarm too, since restart resets the
in-memory baseline.

The fix reads RISCO's own event log instead (``src.risco_client.fetch_events``)
and diffs it against the last-processed alarm-event timestamp - persisted here,
not in memory - so each discrete "Alarm" event gets its own capture regardless
of the system-wide latch, and a process restart mid-session resumes from where
it left off instead of re-baselining. Same atomic load/save shape as
``alarm_scene_config`` / ``display_names``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
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
    value = data.get("last_alarm_event_time") if isinstance(data, dict) else None
    return str(value) if value else None


def save_last_alarm_event_time(value: str, path: Optional[Path] = None) -> None:
    """Atomically persist the last-processed alarm-event timestamp."""

    target = Path(path) if path is not None else CURSOR_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps({"last_alarm_event_time": value}), encoding="utf-8")
    os.replace(tmp, target)
    logger.info("💾 Saved %s", target)
