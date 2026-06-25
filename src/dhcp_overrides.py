"""Persisted per-MAC DHCP category overrides (issue #176).

Maps ``mac`` → category label: the user's manual "this device belongs in that
group" choice for the DHCP reservation planner, made from the webapp instead of
hand-editing ``config/dhcp_plan.json``. The atomic load/save/set logic is shared
verbatim from :mod:`src.display_names` (the same pattern as the network / tuya /
security rename stores) — only the on-disk path differs, so the stores stay in
lock-step.

These overrides are folded **over** ``config/dhcp_plan.json``'s static
``overrides`` (a UI choice beats the committed config), then drive
:func:`src.dhcp_plan.classify`. The real file is gitignored (it would expose a
device inventory in a public repo); a missing file is not an error (empty dict),
the same "graceful default" pattern as the rename stores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "dhcp_overrides.json"


def normalize_mac(mac: str) -> str:
    """Canonical key form: upper-case, colon-separated as reported, trimmed.

    Mirrors :func:`src.dhcp_plan.normalize_mac` so an override keys the same way
    the planner classifies a device, however the AP/router later spells the MAC.
    """
    return (mac or "").strip().upper()


def load_dhcp_overrides(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {mac: category_label} from the config file, or {} if absent.

    Keys are normalised so a lookup matches regardless of the stored casing.
    """
    raw = load_display_names(DEFAULT_PATH if path is None else path)
    return {normalize_mac(k): v for k, v in raw.items()}


def set_dhcp_override(mac: str, category: str, path: Optional[Path] = None) -> None:
    """Set or clear one device's category override, persisting immediately.

    An empty ``category`` clears the override (the device falls back to the
    keyword rules / unassigned).
    """
    set_display_name(normalize_mac(mac), category, DEFAULT_PATH if path is None else path)
