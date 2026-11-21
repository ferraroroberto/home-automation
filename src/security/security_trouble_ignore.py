"""Local "ignore trouble" overrides for RISCO security zones.

Tracks which detectors the user has chosen to ignore the *trouble* flag for, so a
known/accepted trouble (a detector that can't be serviced yet) renders muted and
stops bubbling to the Home/Security main card — while genuinely un-ignored
troubles still surface there.

The persisted id set lives in ``src.devices.id_set_store`` (an ignored detector is
``{"3": "1"}``; un-ignoring clears the entry). The real file is gitignored (it would expose which detectors exist in a
public repo); a missing file is not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.devices.id_set_store import load_id_set, set_id_member

DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "security_trouble_ignore.json"
)


def load_ignored_trouble_zone_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the set of zone ids (as strings) whose trouble is ignored."""
    return load_id_set(DEFAULT_PATH if path is None else path)


def set_zone_trouble_ignored(
    zone_id: str, ignored: bool, path: Optional[Path] = None
) -> None:
    """Mark or unmark one detector's trouble as ignored, persisting immediately."""
    set_id_member(zone_id, ignored, DEFAULT_PATH if path is None else path)
