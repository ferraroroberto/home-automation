"""Persisted runtime state for the alarm-override automation (issue #341).

Tracks, across process/tray restarts, how far the RISCO event log has been
scanned and per-zone trigger counts for the *current* armed session — mirrors
``src/alarm_scene_cursor.py``'s cursor shape (issue #325) but carries the extra
per-zone counters and the set of zones this automation has itself bypassed, so
a restart mid-session doesn't lose count or forget to restore a zone at the
next arm. Same atomic load/save shape as the rest of ``src/*_config.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SESSION_PATH = _CONFIG_DIR / "security_override_session.json"


@dataclass
class OverrideSession:
    """Scan cursor + this session's per-zone trigger counts and auto-bypassed zones."""

    last_event_time: Optional[str] = None
    session_counts: Dict[str, int] = field(default_factory=dict)
    auto_bypassed_zones: List[int] = field(default_factory=list)


def load_override_session(path: Optional[Path] = None) -> OverrideSession:
    """Return the persisted session state, or a fresh one if absent/unreadable."""

    target = Path(path) if path is not None else SESSION_PATH
    if not target.exists():
        return OverrideSession()
    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); starting a fresh session", target, exc)
        return OverrideSession()
    if not isinstance(data, dict):
        return OverrideSession()
    counts = data.get("session_counts")
    zones = data.get("auto_bypassed_zones")
    return OverrideSession(
        last_event_time=str(data["last_event_time"]) if data.get("last_event_time") else None,
        session_counts={str(k): int(v) for k, v in counts.items()} if isinstance(counts, dict) else {},
        auto_bypassed_zones=[int(z) for z in zones] if isinstance(zones, list) else [],
    )


def save_override_session(session: OverrideSession, path: Optional[Path] = None) -> None:
    """Atomically persist the session state."""

    target = Path(path) if path is not None else SESSION_PATH
    write_json_atomic(
        target,
        {
            "last_event_time": session.last_event_time,
            "session_counts": session.session_counts,
            "auto_bypassed_zones": session.auto_bypassed_zones,
        },
    )
