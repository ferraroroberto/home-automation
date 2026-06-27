"""Local hidden-state overrides for Smart Life / Tuya plug + blind rows.

Keyed by ``device_id``: a present key means hidden, a missing key means visible.
Reuses the atomic load/save/set helpers shared by the display-name stores (only
the on-disk path differs), so the rename and hide stores stay in lock-step — the
same relationship ``src.network_hidden`` has with ``src.network_display_names``.
The real file is gitignored (it would expose device ids in a public repo); a
missing file is not an error (empty set).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "tuya_hidden.json"
)

_HIDDEN_MARKER = "1"


def load_hidden_tuya_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the device_ids marked hidden, or an empty set if absent."""
    raw = load_display_names(DEFAULT_PATH if path is None else path)
    return set(raw.keys())


def set_tuya_hidden(device_id: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one Tuya device (plug or blind) as hidden."""
    set_display_name(
        device_id,
        _HIDDEN_MARKER if hidden else "",
        DEFAULT_PATH if path is None else path,
    )
