"""Local display-name overrides for host-visible Wi-Fi radios.

Keys are the same identities used by :mod:`src.network_hidden`: normalised BSSID
first, with an explicit ``SSID:<ssid>`` fallback only when the scan row lacks a
BSSID. The real file is gitignored because it can expose nearby network names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name
from src.network_hidden import normalize_wifi_id

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "network_wifi_display_names.json"
)


def load_network_wifi_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {wifi_id: display_name} from the config file, or {} if absent."""
    raw = load_display_names(DEFAULT_PATH if path is None else path)
    return {normalize_wifi_id(k): v for k, v in raw.items()}


def set_network_wifi_display_name(
    wifi_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one Wi-Fi radio's display-name override."""
    set_display_name(
        normalize_wifi_id(wifi_id),
        display_name,
        DEFAULT_PATH if path is None else path,
    )
