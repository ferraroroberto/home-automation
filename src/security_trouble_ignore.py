"""Local "ignore trouble" overrides for RISCO security zones (issue #225).

Tracks which detectors the user has chosen to ignore the *trouble* flag for, so a
known/accepted trouble (a detector that can't be serviced yet) renders muted and
stops bubbling to the Home/Security main card — while genuinely un-ignored
troubles still surface there.

The on-disk shape is the same ``{zone_id: marker}`` map the display-name/hidden
stores use, so the atomic load/save/set logic is shared verbatim from
``src.display_names`` (only the file path differs), keeping it in lock-step with
``src.security_hidden``. An ignored detector is ``{"3": "1"}``; un-ignoring clears
the entry. The real file is gitignored (it would expose which detectors exist in a
public repo); a missing file is not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "security_trouble_ignore.json"
)

# Truthy marker stored as the map value for an ignored zone (presence is what
# matters; the store only keeps truthy values).
_IGNORED_MARKER = "1"


def load_ignored_trouble_zone_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the set of zone ids (as strings) whose trouble is ignored."""
    return set(load_display_names(DEFAULT_PATH if path is None else path).keys())


def set_zone_trouble_ignored(
    zone_id: str, ignored: bool, path: Optional[Path] = None
) -> None:
    """Mark or unmark one detector's trouble as ignored, persisting immediately."""
    set_display_name(
        zone_id, _IGNORED_MARKER if ignored else "", DEFAULT_PATH if path is None else path
    )
