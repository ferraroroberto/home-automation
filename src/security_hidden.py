"""Local "hidden detector" overrides for RISCO security zones.

Tracks which detectors the user has marked as unused so they drop out of the
default Detectors list (issue #104). The on-disk shape is the same
``{zone_id: value}`` map the display-name stores use, with a truthy marker as the
value — so the atomic load/save/set logic is shared verbatim from
``src.display_names`` (only the file path differs), keeping it in lock-step with
``src.security_display_names`` and the plug/unit equivalents. A hidden detector is
``{"3": "1"}``; un-hiding clears the entry. The real file is gitignored (it would
expose which detectors exist in a public repo); a missing file is not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "security_hidden.json"
)

# Truthy marker stored as the map value for a hidden zone (the store keeps only
# truthy values, so the key's presence is what matters).
_HIDDEN_MARKER = "1"


def load_hidden_zone_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the set of zone ids (as strings) marked hidden, or empty if absent."""
    return set(load_display_names(DEFAULT_PATH if path is None else path).keys())


def set_zone_hidden(
    zone_id: str, hidden: bool, path: Optional[Path] = None
) -> None:
    """Mark or unmark one detector as hidden, persisting immediately."""
    set_display_name(
        zone_id, _HIDDEN_MARKER if hidden else "", DEFAULT_PATH if path is None else path
    )
