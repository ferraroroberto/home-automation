"""Local display-name overrides for Tuya / Smart Life devices.

Maps ``device_id`` → ``display_name``, the plug-side parallel to
``src.display_names`` (the MELCloud unit equivalent). The atomic load/save/set
logic is shared from that module — only the on-disk path differs, so the two
stores stay in lock-step. The real file is gitignored (it would expose room
names in a public repo); a missing file is not an error (empty dict).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "tuya_display_names.json"
)


def load_tuya_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {device_id: display_name} from the config file, or {} if absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def set_tuya_display_name(
    device_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one device's display-name override, persisting immediately."""
    set_display_name(device_id, display_name, DEFAULT_PATH if path is None else path)
