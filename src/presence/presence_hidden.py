"""Local hidden flags for presence people/entities.

Mirrors ``src.security.security_hidden``: hidden ids drop out of the default Presence
list but remain available behind the Show hidden toggle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.devices.id_set_store import load_id_set, set_id_member

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "presence_hidden.json"


def load_hidden_presence_ids(path: Optional[Path] = None) -> Set[str]:
    """Return ids marked hidden, or an empty set when absent."""
    return load_id_set(DEFAULT_PATH if path is None else path)


def set_presence_hidden(entity_id: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one presence entity as hidden."""
    set_id_member(entity_id, hidden, DEFAULT_PATH if path is None else path)
