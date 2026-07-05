"""Local hidden-state overrides for Network tab rows.

Attached devices are keyed by normalised MAC address. Wi-Fi radios are keyed by
normalised BSSID when present, falling back to ``SSID:<ssid>`` only for a scan
row that lacks a BSSID. Both stores use the same atomic map helper as the
display-name stores: truthy value means hidden, missing key means visible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from src._mac import normalize_mac
from src.display_names import load_display_names, set_display_name

DEVICE_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "network_hidden.json"
)
WIFI_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "network_wifi_hidden.json"
)

_HIDDEN_MARKER = "1"


def normalize_wifi_id(bssid: str | None = None, ssid: str | None = None) -> str:
    """Return the stable key used for Wi-Fi radio labels/visibility."""
    raw = (bssid or "").strip()
    if raw.upper().startswith("SSID:"):
        return "SSID:" + raw[5:].strip()
    bssid_key = normalize_mac(bssid or "")
    if bssid_key:
        return bssid_key
    ssid_key = (ssid or "").strip()
    return f"SSID:{ssid_key}" if ssid_key else ""


def load_hidden_device_macs(path: Optional[Path] = None) -> Set[str]:
    """Return normalised MACs marked hidden, or an empty set if absent."""
    raw = load_display_names(DEVICE_DEFAULT_PATH if path is None else path)
    return {normalize_mac(k) for k in raw}


def set_device_hidden(mac: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one attached device as hidden."""
    set_display_name(
        normalize_mac(mac),
        _HIDDEN_MARKER if hidden else "",
        DEVICE_DEFAULT_PATH if path is None else path,
    )


def load_hidden_wifi_ids(path: Optional[Path] = None) -> Set[str]:
    """Return Wi-Fi ids marked hidden, or an empty set if absent."""
    raw = load_display_names(WIFI_DEFAULT_PATH if path is None else path)
    return {normalize_wifi_id(k) for k in raw}


def set_wifi_hidden(wifi_id: str, hidden: bool, path: Optional[Path] = None) -> None:
    """Mark or unmark one Wi-Fi radio/network identity as hidden."""
    key = normalize_wifi_id(wifi_id)
    set_display_name(
        key,
        _HIDDEN_MARKER if hidden else "",
        WIFI_DEFAULT_PATH if path is None else path,
    )
