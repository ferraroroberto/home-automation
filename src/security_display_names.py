"""Local display-name overrides for RISCO security detectors/zones.

Maps ``zone_id`` (as a string) → ``display_name``, the security-side parallel to
``src.display_names`` (the MELCloud unit equivalent) and ``src.tuya_display_names``
(the plug equivalent). RISCO detectors arrive named ``"1"``, ``"2"``, … so a
custom label is the only way to tell them apart at a glance. The atomic
load/save/set logic is shared from ``src.display_names`` — only the on-disk path
differs, so the stores stay in lock-step. The real file is gitignored (it would
expose room names in a public repo); a missing file is not an error (empty dict).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "security_display_names.json"
)


def load_security_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {zone_id: display_name} from the config file, or {} if absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def set_security_display_name(
    zone_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one detector's display-name override, persisting immediately."""
    set_display_name(zone_id, display_name, DEFAULT_PATH if path is None else path)
