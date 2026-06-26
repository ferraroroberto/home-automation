r"""
Shared types + leaf helpers for the home-network core (issue #197 split).
========================================================================
Extracted from ``network_client`` so the three device/probe modules
(``network_host`` / ``network_ap`` / ``network_router``) and the
``network_client`` orchestrator can all import the flattened dataclasses,
exceptions, the static-binding cap, and the two zero-dependency helpers
(``_require`` / ``_normalise_mac``) without importing one another.

This module is a leaf: it imports nothing from the other ``network_*`` modules,
so it can never participate in an import cycle.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #
class NetworkConfigError(RuntimeError):
    """Required NETWORK_* environment variables are missing."""


class NetworkCommandError(RuntimeError):
    """A device rejected a command or returned an unusable response."""


class DhcpBindingTableFull(NetworkCommandError):
    """The router's fixed-size static DHCP-binding table is full (firmware cap).

    A subclass so the batch apply can catch it specifically — stop attempting
    further *new* reservations (every one will fail the same way) while still
    letting slot-neutral replaces through — and report the real reason rather
    than a generic "rejected".
    """


# The F6600P firmware caps its static "DHCP Binding" table at this many rows: the
# (N+1)th create returns ``IF_ERRORID -12`` ("the number of entries has reached
# the maximum limit"). Empirically confirmed against the live unit 2026-06-25.
# Used only to pre-flight the free-slot budget + message; the ``-12`` detection in
# ``_check_binding_result`` is the authoritative backstop if a firmware differs.
DHCP_BIND_MAX = 10


# --------------------------------------------------------------------------- #
# Flattened state                                                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetDevice:
    """One attached client, as reported by an access point / router."""

    mac: str
    ip: Optional[str]
    name: Optional[str]
    conn_type: Optional[str]  # "wired" | "2.4GHz" | "5GHz"
    signal: Optional[int]  # percent (wired reports 100 / None)
    link_rate: Optional[int]  # Mbps as the device reports it
    ssid: Optional[str]
    source: str  # which device(s) reported it: "ap" | "router" | "both"

    @property
    def is_wireless(self) -> bool:
        return bool(self.conn_type) and self.conn_type != "wired"


@dataclass(frozen=True)
class AccessPointHealth:
    """NETGEAR R9000 reachability + identity."""

    reachable: bool
    model: Optional[str] = None
    firmware: Optional[str] = None
    mode: Optional[str] = None  # "access_point" etc.
    device_count: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class RouterHealth:
    """Vodafone ZXHN F6600P reachability, login state, and WAN/internet status.

    The headless login (`RouterClient.login`) and the authenticated WAN-status
    read (issue #129 Phase 3) are both wired. The WAN fields stay ``None`` when
    the router is unreachable, login fails, or the read is rejected — only a
    successful authenticated read populates them.
    """

    reachable: bool
    authenticated: bool = False
    model: str = "ZXHN F6600P"
    error: Optional[str] = None
    # WAN/internet status from the authenticated ZTE data read (Phase 3).
    wan_online: Optional[bool] = None
    public_ip: Optional[str] = None
    gateway: Optional[str] = None
    dns: Optional[str] = None
    connection_name: Optional[str] = None
    uptime_s: Optional[int] = None
    addressing: Optional[str] = None


@dataclass(frozen=True)
class InternetHealth:
    """Host-side view of the WAN - independent of router/AP APIs."""

    online: bool
    gateway_ms: Optional[float] = None
    external_ms: Optional[float] = None
    packet_loss_pct: Optional[float] = None
    download_mbps: Optional[float] = None
    upload_mbps: Optional[float] = None
    speedtest_server: Optional[str] = None


@dataclass(frozen=True)
class WifiBssid:
    """One visible Wi-Fi radio, as seen by the server PC's WLAN adapter."""

    ssid: str
    bssid: str
    signal: Optional[int]
    rssi_dbm: Optional[int]
    channel: Optional[int]
    band: Optional[str]  # "2.4GHz" | "5GHz" | "6GHz"
    radio_type: Optional[str]
    authentication: Optional[str]
    encryption: Optional[str]
    connected: bool = False
    channel_width_mhz: Optional[int] = None


@dataclass(frozen=True)
class WifiChannelScore:
    """Interference score for one candidate Wi-Fi channel.

    Lower is cleaner. The score is based on visible BSSID signal strength and
    channel overlap from the host PC's scan location, so it is a recommendation
    input rather than an RF-site-survey truth.
    """

    channel: int
    score: float
    visible_radios: int
    strongest_signal: Optional[int] = None
    strongest_ssid: Optional[str] = None


@dataclass(frozen=True)
class WifiChannelInsight:
    """Structured channel decision data for one band."""

    band: str
    source: str
    recommended_channel: Optional[int]
    recommended_width_mhz: Optional[int]
    coordinated_channels: tuple[int, ...]
    candidate_scores: tuple[WifiChannelScore, ...]
    rationale: tuple[str, ...] = ()
    apply_supported: bool = False


@dataclass(frozen=True)
class WifiDiagnostics:
    """Best-effort host-side Wi-Fi scan from the machine running the webapp."""

    available: bool
    interface_name: Optional[str] = None
    adapter_description: Optional[str] = None
    current_ssid: Optional[str] = None
    current_bssid: Optional[str] = None
    current_signal: Optional[int] = None
    current_channel: Optional[int] = None
    current_band: Optional[str] = None
    current_radio_type: Optional[str] = None
    bssids: tuple[WifiBssid, ...] = ()
    recommendations: tuple[str, ...] = ()
    insights: tuple[WifiChannelInsight, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class NetworkState:
    """Everything the Network view needs in one snapshot."""

    internet: InternetHealth
    access_point: AccessPointHealth
    router: RouterHealth
    wifi: WifiDiagnostics
    devices: tuple[NetDevice, ...]
    alerts: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Zero-dependency helpers shared by every network module                      #
# --------------------------------------------------------------------------- #
def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise NetworkConfigError(
            f"{name} is not set. Add the NETWORK_* keys to .env "
            "(see config / README)."
        )
    return value


def _normalise_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    hexes = re.findall(r"[0-9a-fA-F]{2}", mac)
    return ":".join(h.upper() for h in hexes) if len(hexes) == 6 else mac.strip().upper()
