"""Offline device identification for the Network view (issue #129 Phase 2).

Two cheap, render-time helpers — no network call, no persistence — that turn a
bare MAC + hostname into something a human recognises:

* :func:`vendor_for_mac` — the manufacturer behind a MAC's OUI prefix, from a
  small **bundled, trimmed** prefix table. The full IEEE registry is ~35 k
  entries; bundling all of it just to label a home LAN is overkill, so this is a
  curated subset of the consumer vendors that actually show up here (the
  maintainer's call in #129 constraint 6: "bundled-trimmed is the low-maintenance
  default"). An unknown prefix returns ``None`` — the UI then falls back to the
  hostname, then the MAC, so a miss is harmless, just less friendly.
* :func:`category_for_device` — a keyword heuristic (hostname + vendor) mapping a
  device to a coarse class (``phone`` / ``computer`` / ``tv`` / ``iot`` / ``nas``
  / ``printer`` / ``router``), which the UI renders as a Lucide glyph. Best-effort
  only; ``unknown`` when nothing matches.

Modern phones rotate a **locally-administered** (randomised) MAC per SSID
(#129 constraint 3); :func:`is_randomized_mac` flags those so the UI can say so
and not pretend the OUI means anything (it doesn't — the vendor bits are random).
"""

from __future__ import annotations

import re
from typing import Optional

# --------------------------------------------------------------------------- #
# OUI → vendor (bundled trimmed subset)                                       #
# --------------------------------------------------------------------------- #
# Keyed by the 24-bit OUI as 6 upper-case hex chars (no separators). Curated for
# the consumer gear common on a home LAN; deliberately small. Add prefixes here
# as real devices surface as "unknown vendor" — accuracy over breadth.
_OUI: dict[str, str] = {
    # Espressif (ESP8266/ESP32 — the bulk of DIY/IoT "n/a" clients)
    "240AC4": "Espressif", "246F28": "Espressif", "30AEA4": "Espressif",
    "5CCF7F": "Espressif", "7C9EBD": "Espressif", "840D8E": "Espressif",
    "84CCA8": "Espressif", "8CAAB5": "Espressif", "A020A6": "Espressif",
    "A4CF12": "Espressif", "B4E62D": "Espressif", "BCDDC2": "Espressif",
    "C44F33": "Espressif", "CC50E3": "Espressif", "DC4F22": "Espressif",
    "ECFABC": "Espressif",
    # Raspberry Pi Foundation
    "B827EB": "Raspberry Pi", "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi",
    "28CDC1": "Raspberry Pi", "D83ADD": "Raspberry Pi", "2CCF67": "Raspberry Pi",
    # Apple
    "28CFDA": "Apple", "3C0754": "Apple", "F0DBE2": "Apple", "DC2B2A": "Apple",
    "ACBC32": "Apple", "88665A": "Apple", "A45E60": "Apple", "7C6D62": "Apple",
    "68AE20": "Apple",
    # Samsung
    "8C7712": "Samsung", "F025B7": "Samsung", "5C0A5B": "Samsung",
    "E8508B": "Samsung", "BC72B1": "Samsung",
    # Google / Nest
    "546009": "Google", "F4F5D8": "Google", "A47733": "Google", "3C5AB4": "Google",
    # Amazon (Echo / Fire)
    "6837E9": "Amazon", "FC65DE": "Amazon", "AC63BE": "Amazon", "44650D": "Amazon",
    # Xiaomi
    "286C07": "Xiaomi", "7811DC": "Xiaomi", "F0B429": "Xiaomi", "64A2F9": "Xiaomi",
    # Sonos
    "000E58": "Sonos", "B8E937": "Sonos", "949F3E": "Sonos", "5CAAFD": "Sonos",
    # Netgear
    "00095B": "Netgear", "9C3DCF": "Netgear", "A040A0": "Netgear", "28C68E": "Netgear",
    # TP-Link
    "50C7BF": "TP-Link", "EC086B": "TP-Link", "A42BB0": "TP-Link",
    # Ubiquiti
    "00156D": "Ubiquiti", "245A4C": "Ubiquiti", "FCECDA": "Ubiquiti", "788A20": "Ubiquiti",
    # Synology
    "001132": "Synology", "0011D8": "Asustek",
    # Intel
    "3CA9F4": "Intel", "7C7A91": "Intel", "A08869": "Intel",
    # Dell
    "180373": "Dell", "B8CA3A": "Dell", "F8BC12": "Dell",
}


def _oui_key(mac: str) -> str:
    """First three octets as 6 upper-case hex chars, or '' if unparseable."""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    return hexed[:6].upper() if len(hexed) >= 6 else ""


def is_randomized_mac(mac: str) -> bool:
    """True if the MAC is locally-administered (a randomised/private address).

    The locally-administered bit is bit 1 of the first octet; a phone rotating
    per-SSID MACs sets it, so the OUI vendor bits are meaningless there.
    """
    key = _oui_key(mac)
    if not key:
        return False
    try:
        first_octet = int(key[:2], 16)
    except ValueError:
        return False
    return bool(first_octet & 0b10)


def vendor_for_mac(mac: str) -> Optional[str]:
    """Manufacturer behind the MAC's OUI, or ``None`` (unknown / randomised)."""
    if is_randomized_mac(mac):
        return None
    return _OUI.get(_oui_key(mac))


# --------------------------------------------------------------------------- #
# Category heuristic                                                          #
# --------------------------------------------------------------------------- #
# (substring, category) pairs matched against "hostname vendor" lower-cased, in
# priority order — the first hit wins, so put the specific signals first.
_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("iphone", "phone"), ("ipad", "phone"), ("android", "phone"),
    ("pixel", "phone"), ("galaxy", "phone"), ("oneplus", "phone"),
    ("-phone", "phone"), ("redmi", "phone"),
    ("appletv", "tv"), ("apple-tv", "tv"), ("chromecast", "tv"),
    ("firetv", "tv"), ("fire-tv", "tv"), ("roku", "tv"), ("bravia", "tv"),
    ("shield", "tv"), ("-tv", "tv"), ("samsungtv", "tv"), ("vizio", "tv"),
    ("printer", "printer"), ("officejet", "printer"), ("laserjet", "printer"),
    ("epson", "printer"), ("canon", "printer"), ("brother", "printer"),
    ("nas", "nas"), ("synology", "nas"), ("diskstation", "nas"),
    ("qnap", "nas"), ("mycloud", "nas"), ("truenas", "nas"),
    ("router", "router"), ("gateway", "router"), ("-ap", "router"),
    ("repeater", "router"), ("extender", "router"), ("mesh", "router"),
    ("deco", "router"), ("eero", "router"), ("netgear", "router"),
    ("ubiquiti", "router"), ("unifi", "router"),
    ("macbook", "computer"), ("imac", "computer"), ("laptop", "computer"),
    ("desktop", "computer"), ("thinkpad", "computer"), ("notebook", "computer"),
    ("surface", "computer"), ("-pc", "computer"), ("workstation", "computer"),
    ("espressif", "iot"), ("esp32", "iot"), ("esp8266", "iot"),
    ("sonoff", "iot"), ("shelly", "iot"), ("tasmota", "iot"), ("tuya", "iot"),
    ("smartlife", "iot"), ("sensor", "iot"), ("-plug", "iot"), ("bulb", "iot"),
    ("lamp", "iot"), ("camera", "iot"), ("-cam", "iot"), ("thermostat", "iot"),
    ("echo", "iot"), ("alexa", "iot"), ("nest", "iot"), ("sonos", "iot"),
    ("raspberry", "computer"),
)

# Weak fallback when no keyword hits: the vendor alone implies a class.
_VENDOR_CATEGORY: dict[str, str] = {
    "Espressif": "iot",
    "Sonos": "iot",
    "Xiaomi": "iot",
    "Synology": "nas",
    "Netgear": "router",
    "TP-Link": "router",
    "Ubiquiti": "router",
    "Raspberry Pi": "computer",
    "Apple": "phone",
    "Intel": "computer",
    "Dell": "computer",
}


def category_for_device(
    name: Optional[str], vendor: Optional[str], conn_type: Optional[str] = None
) -> str:
    """Coarse device class for the row glyph; ``unknown`` when nothing matches."""
    text = " ".join(filter(None, [name, vendor])).lower()
    for needle, category in _KEYWORDS:
        if needle in text:
            return category
    if vendor and vendor in _VENDOR_CATEGORY:
        return _VENDOR_CATEGORY[vendor]
    return "unknown"
