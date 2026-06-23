"""Local hidden flags for presence people/entities.

Mirrors ``src.security_hidden``: hidden ids drop out of the default Presence
list but remain available behind the Show hidden toggle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "presence_hidden.json"
_HIDDEN_MARKER = "1"


def load_hidden_presence_ids(path: Optional[Path] = None) -> Set[str]:
    """Return ids marked hidden, or an empty set when absent."""
    return set(load_display_names(DEFAULT_PATH if path is None else path).keys())


def set_presence_hidden(entity_id: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one presence entity as hidden."""
    set_display_name(
        entity_id, _HIDDEN_MARKER if hidden else "", DEFAULT_PATH if path is None else path
    )
