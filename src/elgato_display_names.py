"""Local display-name overrides for Elgato lights.

Maps stable light identity -> ``display_name``. When a light reports a MAC
address, the key is ``mac:<MAC>`` so labels survive DHCP/IP changes; older
``host:port`` keys are still read as a compatibility fallback. The real file is
gitignored because it can expose room names in a public repo. A missing file is
not an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "elgato_display_names.json"
)


def load_elgato_display_names(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {light_id: display_name} from the config file, or {} if absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def elgato_display_key(light_id: str, mac_address: Optional[str] = None) -> str:
    """Return the durable display-name key for one light."""
    mac = (mac_address or "").strip().upper()
    return f"mac:{mac}" if mac else light_id


def set_elgato_display_name(
    light_id: str, display_name: str, path: Optional[Path] = None
) -> None:
    """Set or clear one light's display-name override, persisting immediately."""
    set_display_name(light_id, display_name, DEFAULT_PATH if path is None else path)
