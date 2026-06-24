"""Local display-name overrides for cameras.

Maps ``camera_id`` -> ``display_name``. The real file is gitignored because it
can expose location names in a public repo. A missing file is not an error.
Reuses the atomic load/save/set store verbatim — only the on-disk path differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "camera_display_names.json"
)


def load_camera_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {camera_id: display_name} from the config file, or {} if absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def set_camera_display_name(
    camera_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one camera's display-name override, persisting immediately."""
    set_display_name(camera_id, display_name, DEFAULT_PATH if path is None else path)
