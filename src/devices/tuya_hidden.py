"""Local hidden-state overrides for Smart Life / Tuya plug + blind rows.

Keyed by ``device_id``: a present key means hidden, a missing key means visible.
The persisted id set lives in ``src.devices.id_set_store``. The real file is gitignored (it would expose device ids in a public repo); a
missing file is not an error (empty set).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.devices.id_set_store import load_id_set, set_id_member

DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "tuya_hidden.json"
)


def load_hidden_tuya_ids(path: Optional[Path] = None) -> Set[str]:
    """Return the device_ids marked hidden, or an empty set if absent."""
    return load_id_set(DEFAULT_PATH if path is None else path)


def set_tuya_hidden(device_id: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one Tuya device (plug or blind) as hidden."""
    set_id_member(device_id, hidden, DEFAULT_PATH if path is None else path)
