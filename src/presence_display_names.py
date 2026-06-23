"""Local display-name overrides for presence people/entities.

Maps a stable webhook person id or Find My entity id to a friendly display
name. The real file is gitignored because names identify household members; a
missing file simply means no overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "presence_display_names.json"
)


def load_presence_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {person_or_entity_id: display_name}, or {} when absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def set_presence_display_name(
    entity_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one presence display-name override."""
    set_display_name(entity_id, display_name, DEFAULT_PATH if path is None else path)
