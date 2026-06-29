"""Local display-name overrides for attached network devices.

Maps ``mac`` (uppercased, colon-separated) → ``display_name``, the network-side
parallel to ``src.display_names`` (the MELCloud unit equivalent),
``src.tuya_display_names`` (plugs) and ``src.security_display_names`` (RISCO
detectors). Most attached clients report an ``n/a`` hostname, so a custom label
is the only way to tell them apart at a glance (issue #129 Phase 2). The atomic
load/save/set logic is shared from ``src.display_names`` — only the on-disk path
differs, so the four stores stay in lock-step. The real file is gitignored (it
would expose a device inventory in a public repo); a missing file is not an
error (empty dict).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src._mac import normalize_mac
from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "network_display_names.json"
)


def load_network_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {mac: display_name} from the config file, or {} if absent.

    Keys are normalised so a lookup matches regardless of the stored casing.
    """
    raw = load_display_names(DEFAULT_PATH if path is None else path)
    return {normalize_mac(k): v for k, v in raw.items()}


def set_network_display_name(
    mac: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one device's display-name override, persisting immediately."""
    set_display_name(normalize_mac(mac), display_name, DEFAULT_PATH if path is None else path)
