"""Local "hidden detector" overrides for RISCO security zones.

Tracks which detectors the user has marked as unused so they drop out of the
default Detectors list. The persisted id set lives in ``src.devices.id_set_store``
(a hidden detector is ``{"3": "1"}``; un-hiding clears the entry). The real file
is gitignored (it would expose which detectors exist in a public repo); a missing
file is not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.devices.id_set_store import load_id_set, set_id_member

DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "security_hidden.json"
)


def load_hidden_zone_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the set of zone ids (as strings) marked hidden, or empty if absent."""
    return load_id_set(DEFAULT_PATH if path is None else path)


def set_zone_hidden(
    zone_id: str, hidden: bool, path: Optional[Path] = None
) -> None:
    """Mark or unmark one detector as hidden, persisting immediately."""
    set_id_member(zone_id, hidden, DEFAULT_PATH if path is None else path)
