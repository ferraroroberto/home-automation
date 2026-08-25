"""Persisted runtime state for the alarm-override automation (issue #341).

Tracks, across process/tray restarts, how far the RISCO event log has been
scanned and per-zone trigger counts for the *current* armed session — mirrors
``src/alarm_scene_cursor.py``'s cursor shape (issue #325) but carries the extra
per-zone counters and the set of zones this automation has itself bypassed, so
a restart mid-session doesn't lose count or forget to restore a zone at the
next arm. Same atomic load/save shape as the rest of ``src/*_config.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import read_json, save_json

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SESSION_PATH = _CONFIG_DIR / "security_override_session.json"


@dataclass
class OverrideSession:
    """Scan cursor + this session's per-zone trigger counts and auto-bypassed zones."""

    last_event_time: Optional[str] = None
    session_counts: Dict[str, int] = field(default_factory=dict)
    auto_bypassed_zones: List[int] = field(default_factory=list)


def load_override_session(path: Optional[Path] = None) -> OverrideSession:
    """Return the persisted session state, or a fresh one if absent.

    Raises :class:`~src._schedule_store.StoreUnreadableError` when the store
    *exists* but can't be read (issue #692) — this is a read-modify-save
    store (``_run_event_scan`` mutates the returned session and saves it
    back), so degrading an unreadable read to a fresh default would erase
    ``auto_bypassed_zones`` and silently leave a detector bypassed forever.
    """

    target = Path(path) if path is not None else SESSION_PATH
    data: Any = read_json(target, None)
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
    save_json(
        target,
        {
            "last_event_time": session.last_event_time,
            "session_counts": session.session_counts,
            "auto_bypassed_zones": session.auto_bypassed_zones,
        },
    )
