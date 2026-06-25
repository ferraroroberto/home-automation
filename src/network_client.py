r"""
Home-network read + control core (async, UI-free)
=================================================
Spike core for issue #125 - the Network view. Mirrors the other domain cores
(``melcloud_client`` / ``sma_client`` / ``risco_client``): no Streamlit, no
FastAPI, just credentials in -> a flattened :class:`NetworkState` out, plus the
reboot controls.

Two devices + one host-side probe feed the state:

* **NETGEAR R9000 access point** (``pynetgear``) - the device inventory
  (per-client MAC / IP / name / signal% / band / SSID) and AP health, and the
  ``reboot_access_point()`` control. Even in AP mode the R9000 reports the whole
  LAN (wired + wireless), so it carries the inventory on its own.
* **Vodafone ZXHN F6600P router** (ZTE) - reached over its SHA256
  challenge-response web login. The login is proven and implemented here
  (:meth:`RouterClient.login`); the authenticated *data reads* (WAN/internet
  status) and ``reboot_router()`` need ZTE's per-request session-token integrity
  scheme and are the documented follow-up (see ``docs/network-spike.md``). The
  router therefore contributes a reachable/authenticated health signal today.
* **Internet health** - measured host-side (ping latency + packet loss, optional
  speed test), independent of either device, so "is the internet up" never
  depends on cracking the router API.

Credentials come from ``.env`` (loopback LAN, never committed)::

    NETWORK_AP_HOST / NETWORK_AP_USERNAME / NETWORK_AP_PASSWORD
    NETWORK_AP_MAC (optional) — stable MAC of the AP; enables auto-rediscovery
    NETWORK_ROUTER_HOST / NETWORK_ROUTER_USERNAME / NETWORK_ROUTER_PASSWORD
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from typing import Mapping, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# External anchor for the "is the internet actually up" probe (Cloudflare DNS).
_EXTERNAL_PROBE_HOST = "1.1.1.1"
# A wireless client below this signal % is surfaced as a weak-link alert.
_WEAK_SIGNAL_PCT = 40
# Packet loss above this % to the outside world is surfaced as an alert.
_HIGH_LOSS_PCT = 5.0
# NETGEAR get_info DeviceMode -> human label (best-effort; 1 == AP here).
_AP_DEVICE_MODE = {"0": "router", "1": "access_point", "2": "bridge", "3": "repeater"}
# Keep the aggregate API under the browser's 30 s request budget even if one
# device API stalls. The individual source tasks still run concurrently.
_INTERNET_TIMEOUT_S = 24.0
_INTERNET_FAST_TIMEOUT_S = 10.0
_ACCESS_POINT_TIMEOUT_S = 12.0
_ACCESS_POINT_REDISCOVER_TIMEOUT_S = 5.0
_ROUTER_TIMEOUT_S = 18.0
_WIFI_TIMEOUT_S = 6.0

# In-memory runtime override for the AP host. None means "use NETWORK_AP_HOST".
# Updated to the last IP that successfully answered; survives within the process
# lifetime so a rediscovered address sticks without touching .env.
_ap_runtime_host: Optional[str] = None


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
# Credentials                                                                 #
# --------------------------------------------------------------------------- #
def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise NetworkConfigError(
            f"{name} is not set. Add the NETWORK_* keys to .env "
            "(see config / README)."
        )
    return value


def _ap_creds() -> tuple[str, str, str]:
    load_dotenv(override=True)
    return (
        _require("NETWORK_AP_HOST"),
        _require("NETWORK_AP_USERNAME"),
        _require("NETWORK_AP_PASSWORD"),
    )


def _ap_effective_host() -> str:
    """Return the runtime-discovered host if one is set, else the configured host."""
    global _ap_runtime_host
    return _ap_runtime_host or _require("NETWORK_AP_HOST")


def _ap_mac() -> Optional[str]:
    """Return the normalised NETWORK_AP_MAC if configured, else None."""
    load_dotenv(override=True)
    raw = (os.getenv("NETWORK_AP_MAC") or "").strip()
    return _normalise_mac(raw) if raw else None


def _rediscover_ap_host(mac: str, leases: list[dict]) -> Optional[str]:
    """Return the IP for *mac* found in *leases*, or None if not present."""
    target = _normalise_mac(mac)
    for lease in leases:
        if _normalise_mac(lease.get("mac")) == target:
            return lease.get("ip") or None
    return None


def _router_creds() -> tuple[str, str, str]:
    load_dotenv(override=True)
    return (
        _require("NETWORK_ROUTER_HOST"),
        _require("NETWORK_ROUTER_USERNAME"),
        _require("NETWORK_ROUTER_PASSWORD"),
    )


# --------------------------------------------------------------------------- #
# Internet health (host-side)                                                 #
# --------------------------------------------------------------------------- #
def _ping(host: str, count: int = 4, timeout_s: int = 2) -> tuple[Optional[float], Optional[float]]:
    """Return (avg_latency_ms, packet_loss_pct) for *host*, or (None, None).

    Uses the OS ``ping`` so no raw-socket privileges are needed; parses both the
    Windows and the POSIX output shapes.
    """
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", str(count), "-w", str(timeout_s * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout_s), host]
    try:
        run_kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": count * timeout_s + 5,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            cmd, **run_kwargs
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("⚠️ ping %s failed: %s", host, exc)
        return None, None

    loss = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet\s*)?loss", out, re.IGNORECASE)
    avg = re.search(r"(?:Average|avg[^=]*=\s*[\d.]+/)\s*=?\s*(\d+(?:\.\d+)?)", out)
    if not avg:  # POSIX "rtt min/avg/max/mdev = 1.2/3.4/..." shape
        avg = re.search(r"=\s*[\d.]+/(\d+(?:\.\d+)?)/", out)
    return (float(avg.group(1)) if avg else None,
            float(loss.group(1)) if loss else None)


def _run_speedtest() -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (download_mbps, upload_mbps, server) via speedtest-cli, or Nones."""
    try:
        import speedtest  # imported lazily: the read path shouldn't pay for it
    except ImportError:
        logger.warning("⚠️ speedtest-cli not installed; skipping throughput test")
        return None, None, None
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        down = st.download() / 1e6
        up = st.upload() / 1e6
        server = st.results.server.get("sponsor")
        return round(down, 1), round(up, 1), server
    except Exception as exc:  # speedtest raises a zoo of its own error types
        logger.warning("⚠️ speed test failed: %s", exc)
        return None, None, None


def _run_quiet(cmd: list[str], timeout: int = 12) -> str:
    """Run a short OS probe without flashing a console window on Windows."""
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs).stdout


async def fetch_internet_health(
    include_speedtest: bool = False, gateway: Optional[str] = None
) -> InternetHealth:
    """Probe WAN reachability/latency host-side; optionally run a speed test."""
    if gateway is None:
        load_dotenv(override=True)
        gateway = (os.getenv("NETWORK_ROUTER_HOST") or "").strip() or None

    ext_ms, loss = await asyncio.to_thread(_ping, _EXTERNAL_PROBE_HOST)
    gw_ms = None
    if gateway:
        gw_ms, _ = await asyncio.to_thread(_ping, gateway, 2)

    down = up = server = None
    if include_speedtest:
        down, up, server = await asyncio.to_thread(_run_speedtest)

    return InternetHealth(
        online=ext_ms is not None,
        gateway_ms=gw_ms,
        external_ms=ext_ms,
        packet_loss_pct=loss,
        download_mbps=down,
        upload_mbps=up,
        speedtest_server=server,
    )


# --------------------------------------------------------------------------- #
# Wi-Fi diagnostics (host-side WLAN adapter via netsh)                         #
# --------------------------------------------------------------------------- #
def _normalise_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    hexes = re.findall(r"[0-9a-fA-F]{2}", mac)
    return ":".join(h.upper() for h in hexes) if len(hexes) == 6 else mac.strip().upper()


def _pct(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d{1,3})", text)
    if not m:
        return None
    return max(0, min(100, int(m.group(1))))


def _int_field(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _quality_to_rssi(signal: Optional[int]) -> Optional[int]:
    # Microsoft WLAN quality is 0..100; this approximation is the conventional
    # analyzer mapping used when the CLI does not expose raw RSSI.
    return int(signal / 2 - 100) if signal is not None else None


def _band_from_channel(channel: Optional[int], raw_band: Optional[str] = None) -> Optional[str]:
    text = (raw_band or "").lower()
    if "6" in text and "ghz" in text:
        return "6GHz"
    if "5" in text and "ghz" in text:
        return "5GHz"
    if "2" in text and ("ghz" in text or "mhz" in text):
        return "2.4GHz"
    if channel is None:
        return None
    if 1 <= channel <= 14:
        return "2.4GHz"
    if 30 <= channel <= 177:
        return "5GHz"
    return None


def _parse_key_value_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        if key:
            out[key] = val.strip()
    return out


def _parse_wifi_interfaces(text: str) -> dict[str, str]:
    fields = _parse_key_value_lines(text)
    # English netsh labels. If Windows is localised, this returns mostly empty
    # and the diagnostics degrades to a clear unavailable note.
    return {
        "name": fields.get("name") or fields.get("interface name"),
        "description": fields.get("description"),
        "state": fields.get("state"),
        "ssid": fields.get("ssid"),
        "bssid": _normalise_mac(fields.get("bssid")) or None,
        "signal": fields.get("signal"),
        "channel": fields.get("channel"),
        "radio_type": fields.get("radio type"),
    }


def _parse_wifi_networks(text: str, current: dict[str, str]) -> list[WifiBssid]:
    networks: list[WifiBssid] = []
    ssid: Optional[str] = None
    auth: Optional[str] = None
    encryption: Optional[str] = None
    current_bssid = _normalise_mac(current.get("bssid"))

    pending: dict[str, Optional[str]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        ssid_match = re.match(r"SSID\s+\d+\s*:\s*(.*)", line, re.IGNORECASE)
        if ssid_match:
            ssid = ssid_match.group(1).strip()
            auth = None
            encryption = None
            pending = None
            continue
        if line.lower().startswith("authentication") and ":" in line:
            auth = line.split(":", 1)[1].strip()
            continue
        if line.lower().startswith("encryption") and ":" in line:
            encryption = line.split(":", 1)[1].strip()
            continue
        bssid_match = re.match(r"BSSID\s+\d+\s*:\s*(.*)", line, re.IGNORECASE)
        if bssid_match:
            pending = {
                "ssid": ssid or "(hidden)",
                "bssid": _normalise_mac(bssid_match.group(1).strip()),
                "authentication": auth,
                "encryption": encryption,
                "signal": None,
                "radio_type": None,
                "band": None,
                "channel": None,
            }
            networks.append(_wifi_from_pending(pending, current_bssid))
            continue
        if pending is None or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "signal":
            pending["signal"] = val
        elif key == "radio type":
            pending["radio_type"] = val
        elif key == "band":
            pending["band"] = val
        elif key == "channel":
            pending["channel"] = val
        networks[-1] = _wifi_from_pending(pending, current_bssid)
    return networks


def _wifi_from_pending(
    raw: Mapping[str, Optional[str]],
    current_bssid: Optional[str],
) -> WifiBssid:
    signal = _pct(raw.get("signal"))
    channel = _int_field(raw.get("channel"))
    bssid = raw.get("bssid") or ""
    return WifiBssid(
        ssid=raw.get("ssid") or "(hidden)",
        bssid=bssid,
        signal=signal,
        rssi_dbm=_quality_to_rssi(signal),
        channel=channel,
        band=_band_from_channel(channel, raw.get("band")),
        radio_type=raw.get("radio_type") or None,
        authentication=raw.get("authentication") or None,
        encryption=raw.get("encryption") or None,
        connected=bool(current_bssid and _normalise_mac(bssid) == current_bssid),
    )


def _wifi_candidate_channels(band: str) -> tuple[int, ...]:
    if band == "2.4GHz":
        # ETSI domain: channels 1-13 are usable in Spain. The scorer still makes
        # the 20 MHz width assumption explicit in the resulting insight.
        return tuple(range(1, 14))
    if band == "5GHz":
        # Common 20 MHz primary channels, including the DFS range currently used
        # by the REDWIFI radio in this home. We do not auto-apply DFS choices.
        return (
            36, 40, 44, 48,
            52, 56, 60, 64,
            100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
        )
    return ()


def _wifi_overlap_weight(band: str, candidate: int, observed: int) -> float:
    distance = abs(candidate - observed)
    if band == "2.4GHz":
        # A 20/22 MHz 2.4 GHz channel spills roughly +/- 4 channel numbers.
        return max(0.0, (5 - distance) / 5) if distance <= 4 else 0.0
    if band == "5GHz":
        if distance == 0:
            return 1.0
        # Without channel-width data, adjacent 20 MHz primaries may or may not
        # share an 80 MHz block. Penalise them lightly but prefer separation.
        return 0.35 if distance <= 4 else 0.0
    return 0.0


def _wifi_channel_scores(band: str, bssids: list[WifiBssid]) -> tuple[WifiChannelScore, ...]:
    scores: list[WifiChannelScore] = []
    radios = [b for b in bssids if b.band == band and b.channel is not None]
    for channel in _wifi_candidate_channels(band):
        score = 0.0
        visible_radios = 0
        strongest_signal: Optional[int] = None
        strongest_ssid: Optional[str] = None
        for b in radios:
            assert b.channel is not None
            signal = b.signal or 0
            weight = _wifi_overlap_weight(band, channel, b.channel)
            if weight <= 0:
                continue
            visible_radios += 1
            score += signal * weight
            if strongest_signal is None or signal > strongest_signal:
                strongest_signal = signal
                strongest_ssid = b.ssid
        scores.append(
            WifiChannelScore(
                channel=channel,
                score=round(score, 1),
                visible_radios=visible_radios,
                strongest_signal=strongest_signal,
                strongest_ssid=strongest_ssid,
            )
        )
    return tuple(scores)


def _best_coordinated_channels(
    scores: tuple[WifiChannelScore, ...],
    band: str,
    count: int = 2,
) -> tuple[int, ...]:
    if count <= 1 or len(scores) < count:
        return ()
    min_distance = 5 if band == "2.4GHz" else 16
    best: tuple[float, tuple[int, ...]] | None = None

    def walk(start: int, selected: tuple[WifiChannelScore, ...]) -> None:
        nonlocal best
        if len(selected) == count:
            channels = tuple(s.channel for s in selected)
            total = round(sum(s.score for s in selected), 1)
            if best is None or (total, channels) < best:
                best = (total, channels)
            return
        for index in range(start, len(scores)):
            candidate = scores[index]
            if all(abs(candidate.channel - chosen.channel) >= min_distance for chosen in selected):
                walk(index + 1, selected + (candidate,))

    walk(0, ())
    return best[1] if best else ()


def _wifi_channel_insights(bssids: list[WifiBssid]) -> tuple[WifiChannelInsight, ...]:
    insights: list[WifiChannelInsight] = []
    for band in ("2.4GHz", "5GHz"):
        scores = _wifi_channel_scores(band, bssids)
        if not scores:
            continue
        best = min(scores, key=lambda s: (s.score, s.channel))
        visible = [b for b in bssids if b.band == band and b.channel is not None]
        width = 20 if band == "2.4GHz" else 40
        coordinated = _best_coordinated_channels(scores, band, 2)
        rationale = [
            f"Scored {len(scores)} candidate channels from {len(visible)} visible {band} radio(s).",
            "Lower score means less visible signal overlapping that channel.",
            "Apply is read-only for now; router/AP channel writes are a separate guarded feature.",
        ]
        if band == "2.4GHz":
            rationale.append("Use 20 MHz width on 2.4 GHz to avoid widening overlap.")
        else:
            rationale.append("Prefer 40 MHz on 5 GHz when stability matters; wider 80 MHz blocks can overlap neighbours.")
        insights.append(
            WifiChannelInsight(
                band=band,
                source="windows_netsh",
                recommended_channel=best.channel,
                recommended_width_mhz=width,
                coordinated_channels=coordinated,
                candidate_scores=tuple(sorted(scores, key=lambda s: (s.score, s.channel))[:8]),
                rationale=tuple(rationale),
            )
        )
    return tuple(insights)


def _is_dfs_5ghz_channel(channel: Optional[int]) -> bool:
    return channel is not None and 52 <= channel <= 144


def _wifi_recommendations(
    current_signal: Optional[int],
    current_band: Optional[str],
    bssids: list[WifiBssid],
    insights: tuple[WifiChannelInsight, ...] = (),
) -> list[str]:
    tips: list[str] = []
    if current_signal is not None:
        if current_signal < 60:
            tips.append(f"Current Wi-Fi signal is weak ({current_signal}%).")
        elif current_signal >= 80:
            tips.append(f"Current Wi-Fi signal is strong ({current_signal}%).")

    by_band: dict[str, list[WifiBssid]] = {"2.4GHz": [], "5GHz": [], "6GHz": []}
    for b in bssids:
        if b.band in by_band:
            by_band[b.band].append(b)

    if len(by_band["2.4GHz"]) >= 6:
        tips.append(
            f"2.4 GHz is crowded ({len(by_band['2.4GHz'])} radios visible); prefer 5 GHz for fixed clients."
        )
    if current_band:
        current = [b for b in by_band.get(current_band, []) if b.connected]
        if current:
            channel = current[0].channel
            competing = [
                b for b in by_band.get(current_band, [])
                if not b.connected and b.channel == channel
            ]
            if competing:
                strongest = max(competing, key=lambda b: b.signal or 0)
                tips.append(
                    f"Channel {channel} has {len(competing)} competing radio(s); strongest is {strongest.ssid}."
                )
    channel_recs: list[str] = []
    for insight in insights:
        if insight.recommended_channel is None:
            continue
        text = (
            f"{insight.band} ch {insight.recommended_channel} "
            f"at {insight.recommended_width_mhz or 20} MHz"
        )
        if insight.band == "5GHz" and _is_dfs_5ghz_channel(insight.recommended_channel):
            text += " (DFS; confirm router/client support)"
        if insight.coordinated_channels:
            text += " (pair " + " + ".join(str(c) for c in insight.coordinated_channels) + ")"
        channel_recs.append(text)
    if channel_recs:
        tips.append("Least-crowded channels: " + "; ".join(channel_recs) + ".")
    return tips[:5]



def _fetch_wifi_diagnostics_sync() -> WifiDiagnostics:
    if not sys.platform.startswith("win"):
        return WifiDiagnostics(available=False, error="Wi-Fi diagnostics are only implemented on Windows.")
    try:
        interfaces = _run_quiet(["netsh", "wlan", "show", "interfaces"])
    except (subprocess.SubprocessError, OSError) as exc:
        return WifiDiagnostics(available=False, error=f"netsh wlan show interfaces failed: {exc}")

    current = _parse_wifi_interfaces(interfaces)
    state = (current.get("state") or "").lower()
    if not current.get("name"):
        return WifiDiagnostics(available=False, error="No Wi-Fi interface reported by Windows.")

    try:
        networks = _run_quiet(["netsh", "wlan", "show", "networks", "mode=bssid"])
    except (subprocess.SubprocessError, OSError) as exc:
        return WifiDiagnostics(
            available=False,
            interface_name=current.get("name"),
            adapter_description=current.get("description"),
            error=f"netsh wlan show networks failed: {exc}",
        )

    current_signal = _pct(current.get("signal"))
    current_channel = _int_field(current.get("channel"))
    current_band = _band_from_channel(current_channel)
    bssids = _parse_wifi_networks(networks, current)
    if current.get("bssid") and not any(b.connected for b in bssids):
        current_mac = current.get("bssid") or ""
        bssids.append(
            WifiBssid(
                ssid=current.get("ssid") or "(current)",
                bssid=current_mac,
                signal=current_signal,
                rssi_dbm=_quality_to_rssi(current_signal),
                channel=current_channel,
                band=current_band,
                radio_type=current.get("radio_type") or None,
                authentication=None,
                encryption=None,
                connected=True,
            )
        )

    insights = _wifi_channel_insights(bssids)
    available = bool(bssids or "connected" in state)
    return WifiDiagnostics(
        available=available,
        interface_name=current.get("name"),
        adapter_description=current.get("description"),
        current_ssid=current.get("ssid") or None,
        current_bssid=current.get("bssid") or None,
        current_signal=current_signal,
        current_channel=current_channel,
        current_band=current_band,
        current_radio_type=current.get("radio_type") or None,
        bssids=tuple(sorted(bssids, key=lambda b: (b.band or "", -(b.signal or 0), b.ssid))),
        recommendations=tuple(_wifi_recommendations(current_signal, current_band, bssids, insights)),
        insights=insights,
        error=None if available else "No Wi-Fi networks visible.",
    )


async def fetch_wifi_diagnostics() -> WifiDiagnostics:
    """Read the server PC's own Wi-Fi view; failures stay local to this block."""
    try:
        return await asyncio.to_thread(_fetch_wifi_diagnostics_sync)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Wi-Fi diagnostics failed: %s", exc)
        return WifiDiagnostics(available=False, error=str(exc))


# --------------------------------------------------------------------------- #
# Access point (NETGEAR R9000 via pynetgear)                                  #
# --------------------------------------------------------------------------- #
def _fetch_ap_sync(host_override: Optional[str] = None) -> tuple[AccessPointHealth, list[NetDevice]]:
    """Blocking pynetgear read: AP identity + the full attached-device list."""
    global _ap_runtime_host
    from pynetgear import Netgear

    _, user, pwd = _ap_creds()
    host = host_override or _ap_effective_host()
    # This R9000 serves the SOAP API on :80 (pynetgear defaults to :5000).
    ng = Netgear(password=pwd, host=host, user=user, port=80)

    info = ng.get_info() or {}
    raw = ng.get_attached_devices_2() or ng.get_attached_devices() or []
    if not info and not raw:
        return AccessPointHealth(reachable=False, error="login or SOAP read failed"), []

    devices: list[NetDevice] = []
    for d in raw:
        dd = d._asdict()
        signal = dd.get("signal")
        link = dd.get("link_rate")
        devices.append(
            NetDevice(
                mac=dd.get("mac"),
                ip=dd.get("ip"),
                name=None if dd.get("name") in ("n/a", "", None) else dd.get("name"),
                conn_type=dd.get("type"),
                signal=int(signal) if str(signal).isdigit() else None,
                link_rate=int(link) if str(link).isdigit() else None,
                ssid=dd.get("ssid") or None,
                source="ap",
            )
        )

    health = AccessPointHealth(
        reachable=True,
        model=info.get("ModelName"),
        firmware=info.get("Firmwareversion"),
        mode=_AP_DEVICE_MODE.get(str(info.get("DeviceMode")), info.get("DeviceMode")),
        device_count=len(devices),
    )
    # Cache the working host so subsequent requests skip the configured IP if
    # it was stale and this was a rediscovery probe with a different address.
    _ap_runtime_host = host
    return health, devices


async def fetch_access_point() -> tuple[AccessPointHealth, list[NetDevice]]:
    """Async wrapper around the blocking pynetgear read."""
    try:
        return await asyncio.to_thread(_fetch_ap_sync)
    except NetworkConfigError:
        raise
    except Exception as exc:
        logger.warning("⚠️ access-point read failed: %s", exc)
        return AccessPointHealth(reachable=False, error=str(exc)), []


async def resolve_ip_by_mac(mac: str) -> Optional[str]:
    """Best-effort current IP for a device MAC, from the AP's attached devices.

    Lets other domains (e.g. cameras, issue #190) self-heal a stale configured
    IP the way the AP and Tuya plugs already do. Returns None when the AP read
    fails, isn't configured, or the MAC isn't present — callers treat that as
    "no recovery available" and leave the device flagged unreachable.
    """
    target = _normalise_mac(mac)
    if not target:
        return None
    try:
        _health, devices = await fetch_access_point()
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        logger.info("ℹ️ MAC→IP resolve via AP failed: %s", exc)
        return None
    for dev in devices:
        if _normalise_mac(dev.mac) == target and dev.ip:
            return dev.ip
    return None


def reboot_access_point() -> None:
    """Reboot the NETGEAR R9000 (proven working via pynetgear)."""
    from pynetgear import Netgear

    _, user, pwd = _ap_creds()
    host = _ap_effective_host()
    ng = Netgear(password=pwd, host=host, user=user, port=80)
    if not ng.reboot():
        raise NetworkCommandError("access point rejected the reboot command")
    logger.info("ℹ️ access-point reboot command accepted")


# --------------------------------------------------------------------------- #
# Router (Vodafone ZXHN F6600P / ZTE)                                         #
# --------------------------------------------------------------------------- #
class RouterClient:
    """Thin ZTE ZXHN web client - SHA256 challenge-response login.

    The login flow (reverse-engineered from the live login JS, proven against
    firmware on the unit):

    1. ``GET /?_type=loginData&_tag=login_entry`` -> JSON ``sess_token``
    2. ``GET /?_type=loginData&_tag=login_token`` -> XML challenge token
    3. ``POST /?_type=loginData&_tag=login_entry`` with
       ``Password = sha256(password + challenge)`` -> ``login_need_refresh: true``

    Authenticated data reads + reboot need ZTE's per-request session-token
    integrity scheme; that is the issue #125 follow-up.
    """

    def __init__(self, host: str, user: str, password: str) -> None:
        self._base = f"http://{host}"
        self._user = user
        self._password = password
        self._session = requests.Session()
        self._session.headers["Referer"] = self._base + "/"

    def login(self, timeout: int = 8) -> bool:
        """Perform the challenge-response login. Returns True on success."""
        s, base = self._session, self._base
        s.get(base + "/", timeout=timeout)
        try:
            sess_token = s.get(
                base + "/?_type=loginData&_tag=login_entry", timeout=timeout
            ).json()["sess_token"]
            challenge_xml = s.get(
                base + "/?_type=loginData&_tag=login_token", timeout=timeout
            ).text
        except (ValueError, KeyError, requests.RequestException) as exc:
            raise NetworkCommandError(f"router login handshake failed: {exc}") from exc

        match = re.search(r">([^<>]+)<", challenge_xml)
        if not match:
            raise NetworkCommandError("router login challenge token not found")
        hashed = hashlib.sha256(
            (self._password + match.group(1).strip()).encode()
        ).hexdigest()

        resp = s.post(
            base + "/?_type=loginData&_tag=login_entry",
            data={
                "action": "login",
                "Password": hashed,
                "Username": self._user,
                "_sessionTOKEN": sess_token,
            },
            timeout=timeout,
        )
        try:
            ok = bool(resp.json().get("login_need_refresh"))
        except ValueError:
            ok = False
        if ok:
            s.get(base + "/", timeout=timeout)  # land the authenticated session
        return ok

    # ---- authenticated data read + reboot (issue #129 Phase 3) ------------- #
    _WAN_FEED = (
        "/?_type=menuData&_tag=wan_internetstatus_lua.lua&TypeUplink=2&pageType=1"
    )
    _REBOOT_FEED = "/?_type=menuData&_tag=devmgr_restartmgr_lua.lua"
    # The LAN attached/allocated-address table — same page-gated menuData pattern
    # as the WAN feed, gated behind the "localNetStatus" menu page. It carries the
    # router-side hostnames (HostName/IPAddress/MACAddress) the AP read lacks.
    _LAN_DEVS_FEED = "/?_type=menuData&_tag=accessdev_landevs_lua.lua"

    def _menu_view(self, tag: str, timeout: int = 10) -> str:
        """Load a menu page so its data feed is unlocked server-side.

        The firmware gates each ``menuData`` feed on the *current* menu page, so a
        feed read returns ``SessionTimeout`` unless that page was loaded first.
        """
        return self._session.get(
            f"{self._base}/?_type=menuView&_tag={tag}",
            headers={"Referer": self._base + "/"},
            timeout=timeout,
        ).text

    def read_wan(self, timeout: int = 10) -> dict:
        """Return the live internet WAN instance as a dict, or ``{}`` if none up.

        Requires an authenticated session (call :meth:`login` first). Raises
        :class:`NetworkCommandError` if the read itself is rejected.
        """
        self._menu_view("ethWanStatus", timeout)
        body = self._session.get(
            self._base + self._WAN_FEED,
            headers={"Referer": f"{self._base}/?_type=menuView&_tag=ethWanStatus"},
            timeout=timeout,
        ).text
        if "SessionTimeout" in body or "404 Not Found" in body:
            raise NetworkCommandError("router WAN read rejected (session/page)")
        return _pick_internet_wan(_parse_instances(body))

    def read_dhcp_leases(self, timeout: int = 10) -> list[dict]:
        """Return the router's LAN allocated-address table for hostname enrichment.

        Each entry is ``{mac, ip, hostname}`` (an empty ``HostName`` → ``None``).
        Requires an authenticated session (call :meth:`login` first); page-gated
        exactly like :meth:`read_wan`. Raises :class:`NetworkCommandError` if the
        read itself is rejected. The ZTE firmware exposes no DHCP port / lease-time
        feed (every candidate 404s), so only host/ip/mac are available.
        """
        self._menu_view("localNetStatus", timeout)
        body = self._session.get(
            self._base + self._LAN_DEVS_FEED,
            headers={"Referer": f"{self._base}/?_type=menuView&_tag=localNetStatus"},
            timeout=timeout,
        ).text
        if "SessionTimeout" in body or "404 Not Found" in body:
            raise NetworkCommandError("router DHCP read rejected (session/page)")
        leases: list[dict] = []
        for inst in _parse_instances(body):
            mac = inst.get("MACAddress")
            if not mac:
                continue
            leases.append({
                "mac": mac,
                "ip": inst.get("IPAddress") or None,
                "hostname": inst.get("HostName") or None,
            })
        return leases

    # ---- DHCP binding write-back — phase 2 (issue #176) -------------------- #
    # The reservation *planner* (src.dhcp_plan) computes a MAC→IP assignment; this
    # is the opt-in write-back that pushes it to the router's static "DHCP Binding"
    # table. The table lives on the lanMgrIpv4 LAN page, feed
    # Localnet_LanMgrIpv4_DHCPStaticRule_lua.lua, object OBJ_DHCPBIND_ID. Reads are
    # page-gated exactly like read_dhcp_leases; writes reuse the commConf.IntegCheck
    # POST proven by reboot() (create = IF_ACTION=Apply with _InstID=-1; delete =
    # IF_ACTION=Delete by the row's _InstID path; rolling _sessionTOKEN + RSA Check
    # header). Verified live with an add→read→delete round-trip on the F6600P.
    _DHCP_BIND_PAGE = "lanMgrIpv4"
    _DHCP_BIND_FEED = (
        "/?_type=menuData&_tag=Localnet_LanMgrIpv4_DHCPStaticRule_lua.lua"
    )
    # encodeURIComponent's unreserved set — the web UI signs the URL-encoded body,
    # so we must encode field values exactly as the browser would before hashing.
    _URI_UNRESERVED = "-_.!~*'()"

    def read_dhcp_bindings(self, timeout: int = 10) -> list[dict]:
        """Return the router's existing static DHCP bindings.

        Each entry is ``{name, mac, ip, inst_id}`` — ``inst_id`` is the firmware's
        instance path (e.g. ``DEV.V4DP.Sr.Pl1.Bd1``) needed to delete/replace the
        row. Requires an authenticated session (call :meth:`login` first); page-
        gated exactly like :meth:`read_dhcp_leases`. Raises
        :class:`NetworkCommandError` if the read itself is rejected.
        """
        self._menu_view(self._DHCP_BIND_PAGE, timeout)
        body = self._session.get(
            self._base + self._DHCP_BIND_FEED,
            headers={"Referer": f"{self._base}/?_type=menuView&_tag={self._DHCP_BIND_PAGE}"},
            timeout=timeout,
        ).text
        if "SessionTimeout" in body or "404 Not Found" in body:
            raise NetworkCommandError("router DHCP-binding read rejected (session/page)")
        out: list[dict] = []
        for inst in _parse_instances(body):
            mac, ip = inst.get("MACAddr"), inst.get("IPAddr")
            if not mac or not ip:
                continue
            out.append({
                "name": inst.get("Name") or None,
                "mac": mac,
                "ip": ip,
                "inst_id": inst.get("_InstID") or None,
            })
        return out

    def write_dhcp_binding(
        self, name: str, mac: str, ip: str, timeout: int = 10
    ) -> bool:
        """Add (or replace) one Name/MAC/IP static binding via the integrity POST.

        Creates a new reservation (``_InstID=-1``). If the MAC already has a
        binding its row is deleted first, so the write is an idempotent replace.
        Returns ``True`` on the firmware's ``SUCC``; raises
        :class:`NetworkCommandError` with a **distinct** message for a session/
        token failure vs a field-validation reject.
        """
        norm = _normalise_mac(mac)
        prior = next(
            (
                b
                for b in self.read_dhcp_bindings(timeout)
                if _normalise_mac(b["mac"]) == norm and b.get("inst_id")
            ),
            None,
        )
        return self._write_binding(name, mac, ip, prior, timeout)

    def _write_binding(
        self,
        name: str,
        mac: str,
        ip: str,
        prior: Optional[Mapping[str, str]],
        timeout: int = 10,
    ) -> bool:
        """Create one binding, first deleting ``prior`` (the existing row for this
        MAC) when given — an idempotent replace. The caller passes ``prior`` from a
        table read it already did, so the batch apply re-reads the table once, not
        once per row (the firmware is slow; per-row reads are what made a bulk apply
        "wait a lot"). Raises :class:`DhcpBindingTableFull` when the table is full.
        """
        if prior and prior.get("inst_id"):
            self._delete_dhcp_binding(prior["inst_id"], timeout)
        body = (
            "IF_ACTION=Apply&_InstID=-1"
            f"&Name={quote(name, safe=self._URI_UNRESERVED)}"
            f"&MACAddr={quote(mac, safe=self._URI_UNRESERVED)}"
            f"&IPAddr={quote(ip, safe=self._URI_UNRESERVED)}"
        )
        return self._check_binding_result(self._binding_post(body, timeout), "write")

    def _delete_dhcp_binding(self, inst_id: str, timeout: int = 10) -> bool:
        """Delete one static binding by its firmware ``_InstID`` path."""
        body = f"IF_ACTION=Delete&_InstID={inst_id}"
        return self._check_binding_result(self._binding_post(body, timeout), "delete")

    def _binding_post(self, body: str, timeout: int = 10) -> str:
        """Integrity-checked POST to the DHCP-binding feed (create / delete).

        Reuses the ``commConf.IntegCheck`` scheme proven by :meth:`reboot`: load
        the LAN page (which rotates ``_sessionTOKEN``), sign ``body`` + the rolling
        token with the RSA ``Check`` header, POST to the static-rule feed, and
        return the raw response text.
        """
        s, base = self._session, self._base
        home = s.get(base + "/", timeout=timeout).text
        pubkey = _extract_pubkey(home)  # the PEM lives on the home frame
        page = self._menu_view(self._DHCP_BIND_PAGE, timeout)  # rotates the token
        token = _extract_token(page) or _extract_token(home)
        if not token or not pubkey:
            raise NetworkCommandError("router binding write: missing session token or key")
        full = f"{body}&_sessionTOKEN={token}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{base}/?_type=menuView&_tag={self._DHCP_BIND_PAGE}",
            "Check": _asy_encode(pubkey, hashlib.sha256(full.encode()).hexdigest()),
        }
        return s.post(
            base + self._DHCP_BIND_FEED, data=full, headers=headers, timeout=timeout
        ).text

    @staticmethod
    def _check_binding_result(text: str, action: str) -> bool:
        """Map a binding POST response to True / a distinct ``NetworkCommandError``.

        Distinguishes the conditions that need different handling: a session/token
        expiry, the firmware's full-table cap (``IF_ERRORID -12`` →
        :class:`DhcpBindingTableFull`, so the batch stops hammering), and any other
        field-validation reject. The ``IF_ERRORSTR`` is HTML-unescaped so the
        surfaced message is readable (the firmware encodes spaces as ``&#32;``).
        """
        if "SessionTimeout" in text:
            raise NetworkCommandError(
                f"router rejected DHCP-binding {action} (session/token expired)"
            )
        err = re.search(r"<IF_ERRORSTR>(.*?)</IF_ERRORSTR>", text)
        code = html.unescape(err.group(1)).strip() if err else ""
        if code == "SUCC":
            return True
        err_id = re.search(r"<IF_ERRORID>(-?\d+)</IF_ERRORID>", text)
        if (err_id and err_id.group(1) == "-12") or "maximum limit" in code.lower():
            raise DhcpBindingTableFull(
                f"router's DHCP reservation table is full — the router holds at "
                f"most {DHCP_BIND_MAX} reservations; delete some to add more"
            )
        raise NetworkCommandError(
            f"router rejected DHCP-binding {action}: {code or 'no IF_ERRORSTR in response'}"
        )

    def reboot(self, timeout: int = 10) -> None:
        """Reboot the router via the authenticated POST + RSA integrity header.

        The web UI gates writes behind ``commConf.IntegCheck``: each POST carries
        the rolling ``_sessionTOKEN`` in the body plus a ``Check`` header =
        base64(RSA-PKCS1v15(sha256(body))) under the page's embedded public key.
        The device drops the connection as it restarts, so a post-accept transport
        error is treated as success, not failure.
        """
        s, base = self._session, self._base
        home = s.get(base + "/", timeout=timeout).text
        pubkey = _extract_pubkey(home)  # the PEM lives only on the home frame
        # Loading the reboot page ROTATES the session token: the POST must carry
        # the token embedded in *that* page's response, not the earlier home one
        # (using the stale token gets "this page has expired"). Verified live.
        page = self._menu_view("rebootAndReset", timeout)
        token = _extract_token(page) or _extract_token(home)
        if not token or not pubkey:
            raise NetworkCommandError("router reboot: missing session token or key")
        body = f"IF_ACTION=Restart&_sessionTOKEN={token}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"{base}/?_type=menuView&_tag=rebootAndReset",
            "Check": _asy_encode(pubkey, hashlib.sha256(body.encode()).hexdigest()),
        }
        try:
            text = s.post(base + self._REBOOT_FEED, data=body, headers=headers,
                          timeout=timeout).text
        except requests.RequestException as exc:
            logger.info("ℹ️ router connection dropped during reboot (%s) — treating as accepted", exc)
            return
        if "SessionTimeout" in text:
            raise NetworkCommandError("router rejected reboot (session/token)")
        err = re.search(r"<IF_ERRORSTR>(.*?)</IF_ERRORSTR>", text)
        if err and err.group(1) != "SUCC":
            raise NetworkCommandError(f"router rejected reboot: {err.group(1)}")


# --------------------------------------------------------------------------- #
# Router parsing + integrity helpers (issue #129 Phase 3)                     #
# --------------------------------------------------------------------------- #
def _parse_instances(xml: str) -> list[dict]:
    """Parse ``<Instance>`` blocks into ParaName→ParaValue dicts (XML-unescaped)."""
    out: list[dict] = []
    for block in re.findall(r"<Instance>(.*?)</Instance>", xml, re.DOTALL):
        names = re.findall(r"<ParaName>(.*?)</ParaName>", block)
        vals = re.findall(r"<ParaValue>(.*?)</ParaValue>", block)
        if len(names) == len(vals):
            out.append({n: html.unescape(v) for n, v in zip(names, vals)})
    return out


def _merge_router_leases(
    devices: list[NetDevice], leases: list[dict]
) -> list[NetDevice]:
    """Fold the router DHCP table into the AP inventory, keyed by normalized MAC.

    For a device seen by both the AP and the router: fill a missing ``name`` from
    the router hostname and mark ``source="both"`` (the AP never overwrites a name
    it already reported). A lease with no AP match becomes a router-only
    ``NetDevice`` (``conn_type``/``signal`` unknown, ``source="router"``) — these
    are the wired clients the AP can't see.
    """
    merged = list(devices)
    by_mac = {_normalise_mac(d.mac): i for i, d in enumerate(merged) if d.mac}
    for lease in leases:
        key = _normalise_mac(lease.get("mac"))
        if not key:
            continue
        host = lease.get("hostname") or None
        idx = by_mac.get(key)
        if idx is not None:
            d = merged[idx]
            merged[idx] = replace(
                d, name=d.name if d.name else host, source="both"
            )
        else:
            merged.append(NetDevice(
                mac=lease.get("mac"),
                ip=lease.get("ip"),
                name=host,
                conn_type=None,
                signal=None,
                link_rate=None,
                ssid=None,
                source="router",
            ))
    return merged


def _pick_internet_wan(instances: list[dict]) -> dict:
    """The live internet WAN = connected with a real IPv4; prefer default-gateway."""
    live = [
        d for d in instances
        if d.get("ConnStatus") == "Connected"
        and d.get("IPAddress", "0.0.0.0") not in ("", "0.0.0.0")
    ]
    if not live:
        return {}
    live.sort(key=lambda d: d.get("IsDefGW") == "1", reverse=True)
    return live[0]


def _extract_token(html_text: str) -> Optional[str]:
    """The rolling per-request session token the page embeds as ``\\xNN`` escapes."""
    m = re.search(r'_sessionTmpToken\s*=\s*"((?:\\x[0-9a-fA-F]{2})+)"', html_text)
    if not m:
        return None
    return bytes(int(h, 16) for h in re.findall(r"\\x([0-9a-fA-F]{2})", m.group(1))).decode("latin-1")


def _extract_pubkey(html_text: str) -> Optional[str]:
    """The PEM RSA public key embedded in ``asyEncode()`` (``\\n`` → real newlines)."""
    m = re.search(
        r'pubKey\s*=\s*"(-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----)"',
        html_text, re.DOTALL,
    )
    return m.group(1).replace("\\n", "\n") if m else None


def _asy_encode(pem: str, src: str) -> str:
    """``asyEncode``: RSA PKCS#1 v1.5 encrypt of *src* under *pem*, base64 (JSEncrypt)."""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = load_pem_public_key(pem.encode())
    return base64.b64encode(key.encrypt(src.encode(), padding.PKCS1v15())).decode()


def _wan_dns(wan: dict) -> Optional[str]:
    parts = [p for p in (wan.get("DNS1"), wan.get("DNS2")) if p and p not in ("0.0.0.0", "::")]
    return ", ".join(parts) or None


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fetch_router_sync() -> tuple[RouterHealth, list[dict]]:
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    try:
        authed = client.login()
    except NetworkCommandError as exc:
        return RouterHealth(reachable=True, authenticated=False, error=str(exc)), []
    except requests.RequestException as exc:
        return RouterHealth(reachable=False, error=str(exc)), []
    if not authed:
        return RouterHealth(reachable=True, authenticated=False), []
    # Authenticated → layer on the WAN/internet status (best-effort: a read
    # failure leaves the WAN fields None rather than dropping the login signal).
    try:
        wan = client.read_wan()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ router WAN read failed: %s", exc)
        wan = {}
    health = RouterHealth(
        reachable=True,
        authenticated=True,
        wan_online=bool(wan),
        public_ip=wan.get("IPAddress") or None,
        gateway=wan.get("GateWay") or None,
        dns=_wan_dns(wan),
        connection_name=wan.get("WANCName") or None,
        uptime_s=_to_int(wan.get("UpTime")),
        addressing=wan.get("Addressingtype") or None,
    )
    # DHCP lease table for hostname enrichment (best-effort, same authenticated
    # session: a read failure leaves the inventory AP-only, never drops it).
    try:
        leases = client.read_dhcp_leases()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ router DHCP-lease read failed: %s", exc)
        leases = []
    return health, leases


async def fetch_router() -> tuple[RouterHealth, list[dict]]:
    """Async wrapper: router health + its DHCP lease table for hostname merge."""
    try:
        return await asyncio.to_thread(_fetch_router_sync)
    except NetworkConfigError:
        raise
    except Exception as exc:
        logger.warning("⚠️ router read failed: %s", exc)
        return RouterHealth(reachable=False, error=str(exc)), []


def reboot_router() -> None:
    """Reboot the Vodafone ZXHN F6600P over its authenticated web API (Phase 3).

    Logs in, then issues the integrity-checked restart POST. The device drops all
    connections and takes ~5 min to come back — strictly a deliberate, confirmed
    user action (the UI gates it behind a styled confirm).
    """
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    if not client.login():
        raise NetworkCommandError("router login failed; cannot reboot")
    client.reboot()
    logger.info("ℹ️ router reboot command accepted")


# --------------------------------------------------------------------------- #
# DHCP reservation write-back (issue #176)                                    #
# --------------------------------------------------------------------------- #
def _fetch_dhcp_bindings_sync() -> list[dict]:
    host, user, pwd = _router_creds()
    # Retry once on failure: the F6600P tolerates only so many concurrent logins,
    # and the 15 s Network-tab poll logs in on its own cadence — so a binding read
    # fired at the same moment can lose the race. A fresh login after a short pause
    # almost always wins the second time. Without this, a transient miss degrades the
    # whole plan to "bindings unknown" (no reservation list, misleading status).
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        client = RouterClient(host, user, pwd)
        try:
            if not client.login():
                raise NetworkCommandError("router login failed; cannot read DHCP bindings")
            return client.read_dhcp_bindings()
        except (NetworkCommandError, requests.RequestException) as exc:
            last_exc = exc
            logger.warning("⚠️ DHCP-binding read attempt %d/2 failed: %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(1.5)
    raise last_exc if last_exc else NetworkCommandError("DHCP-binding read failed")


async def fetch_dhcp_bindings() -> list[dict]:
    """Async: the router's static-binding table (``[{name, mac, ip, inst_id}]``).

    Raises on a login/read failure — the planner falls back to a binding-less plan
    rather than presenting a misleading one as already-applied.
    """
    return await asyncio.to_thread(_fetch_dhcp_bindings_sync)


def _add_bindings_on_client(
    client: "RouterClient", rows: list[Mapping[str, str]]
) -> list[dict]:
    """Write each ``{name, mac, ip}`` add-row on an **already-logged-in** client.

    Reads the table ONCE, up front (reused to find a MAC's existing row so a replace
    is delete+add, and to know free slots), then writes cap-aware: a create that
    can't fit is skipped rather than hammered. Shared by the single-batch apply and
    the combined remove+add apply (issue #176) — the latter calls this *after* its
    deletes, so the freed slots are already reflected in this fresh table read.
    """
    existing = client.read_dhcp_bindings()
    by_mac = {_normalise_mac(b["mac"]): b for b in existing if b.get("mac")}
    free = max(0, DHCP_BIND_MAX - len(existing))
    table_full = free <= 0
    results: list[dict] = []
    for row in rows:
        name, mac, ip = row.get("name") or "", row.get("mac") or "", row.get("ip") or ""
        norm = _normalise_mac(mac)
        prior = by_mac.get(norm)
        # A replace (MAC already bound) deletes then re-adds — slot-neutral. Only a
        # genuinely new reservation consumes a slot, so only it can hit the cap.
        is_create = not (prior and prior.get("inst_id"))
        if is_create and table_full:
            results.append({
                "mac": mac, "ip": ip, "ok": False, "skipped": True,
                "error": (
                    f"router reservation table is full — it holds at most "
                    f"{DHCP_BIND_MAX} reservations; delete some to add more"
                ),
            })
            continue
        try:
            client._write_binding(name, mac, ip, prior)
            results.append({"mac": mac, "ip": ip, "ok": True, "error": None})
            by_mac[norm] = {"name": name, "mac": mac, "ip": ip, "inst_id": None}
            if is_create:
                free -= 1
            logger.info("ℹ️ DHCP binding written: %s → %s", mac, ip)
        except DhcpBindingTableFull as exc:
            # The cap was reached (lower than our estimate, or the table grew under
            # us). Record the honest reason, stop attempting *new* reservations, but
            # keep going so any remaining slot-neutral replaces still apply.
            table_full = True
            results.append({"mac": mac, "ip": ip, "ok": False, "error": str(exc)})
            logger.warning("⚠️ DHCP table full at %s → %s: %s", mac, ip, exc)
        except NetworkCommandError as exc:
            # One bad row never aborts the batch — record it and keep going.
            results.append({"mac": mac, "ip": ip, "ok": False, "error": str(exc)})
            logger.warning("⚠️ DHCP binding failed for %s → %s: %s", mac, ip, exc)
    return results


def _apply_dhcp_bindings_sync(rows: list[Mapping[str, str]]) -> list[dict]:
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    if not client.login():
        raise NetworkCommandError("router login failed; cannot apply DHCP bindings")
    return _add_bindings_on_client(client, rows)


async def apply_dhcp_bindings(rows: list[Mapping[str, str]]) -> list[dict]:
    """Async: write each ``{name, mac, ip}`` binding row-by-row, never auto.

    Logs in once and applies the rows on that one session. Returns a per-row
    ``[{mac, ip, ok, error}]`` result; a mid-batch failure is recorded, not raised,
    so a single rejected row can't silently drop the rest. **Only ever called from
    an explicit, confirm-gated user action** — never on a poll.
    """
    return await asyncio.to_thread(_apply_dhcp_bindings_sync, rows)


def _apply_dhcp_changes_sync(
    remove_inst_ids: list[str], add_rows: list[Mapping[str, str]]
) -> list[dict]:
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    if not client.login():
        raise NetworkCommandError("router login failed; cannot apply DHCP changes")
    results: list[dict] = []
    # Deletes FIRST, on this one session: each frees a slot, so the add pass (which
    # re-reads the table) sees the freed space and a create that previously wouldn't
    # fit now does. A failed delete is recorded, not raised — the adds still run.
    for inst_id in remove_inst_ids:
        try:
            client._delete_dhcp_binding(inst_id)
            results.append({"op": "remove", "inst_id": inst_id, "ok": True, "error": None})
            logger.info("ℹ️ DHCP binding deleted: %s", inst_id)
        except NetworkCommandError as exc:
            results.append({"op": "remove", "inst_id": inst_id, "ok": False, "error": str(exc)})
            logger.warning("⚠️ DHCP delete failed for %s: %s", inst_id, exc)
    add_results = _add_bindings_on_client(client, add_rows)
    for r in add_results:
        r.setdefault("op", "add")
    results.extend(add_results)
    return results


async def apply_dhcp_changes(
    remove_inst_ids: list[str], add_rows: list[Mapping[str, str]]
) -> list[dict]:
    """Async: apply a staged batch of reservation changes in one router session.

    Deletes ``remove_inst_ids`` first (freeing slots), then writes ``add_rows``
    (``{name, mac, ip}``) cap-aware — so the whole "remove these, add those" plan
    goes in one sequence rather than the user juggling per-row writes (issue #176).
    Returns a combined per-op result list (each row tagged ``op`` = ``remove`` /
    ``add``); a single failure is recorded, not raised. **Only ever called from an
    explicit, confirm-gated user action** — never on a poll.
    """
    return await asyncio.to_thread(
        _apply_dhcp_changes_sync, list(remove_inst_ids), list(add_rows)
    )


def _delete_dhcp_binding_sync(inst_id: str) -> bool:
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    if not client.login():
        raise NetworkCommandError("router login failed; cannot delete DHCP binding")
    ok = client._delete_dhcp_binding(inst_id)
    logger.info("ℹ️ DHCP binding deleted: %s", inst_id)
    return ok


async def delete_dhcp_binding(inst_id: str) -> bool:
    """Async: delete one static reservation by its firmware ``_InstID`` path.

    Frees a slot in the fixed-size binding table so a new reservation can be
    written — the user-facing answer to the 10-entry cap (delete a row held by an
    offline/retired device, then add a new one). **Only ever called from an
    explicit, confirm-gated user action** — never on a poll. Raises
    :class:`NetworkCommandError` on a login/reject.
    """
    return await asyncio.to_thread(_delete_dhcp_binding_sync, inst_id)


# --------------------------------------------------------------------------- #
# Aggregate                                                                   #
# --------------------------------------------------------------------------- #
def _derive_alerts(
    internet: InternetHealth,
    ap: AccessPointHealth,
    router: RouterHealth,
    devices: list[NetDevice],
) -> list[str]:
    alerts: list[str] = []
    if not internet.online:
        alerts.append("Internet appears DOWN (no reply from the outside world).")
    if internet.packet_loss_pct and internet.packet_loss_pct >= _HIGH_LOSS_PCT:
        alerts.append(f"High packet loss to the internet: {internet.packet_loss_pct:.0f}%.")
    if not ap.reachable:
        alerts.append(f"Access point unreachable: {ap.error or 'no response'}.")
    if not router.reachable:
        alerts.append(f"Router unreachable: {router.error or 'no response'}.")
    weak = [d for d in devices if d.is_wireless and d.signal is not None and d.signal < _WEAK_SIGNAL_PCT]
    if weak:
        alerts.append(
            f"{len(weak)} wireless client(s) on weak signal (<{_WEAK_SIGNAL_PCT}%)."
        )
    return alerts


async def _with_timeout(label: str, coro, timeout_s: float, fallback):
    """Return *fallback* if one source exceeds its budget.

    ``asyncio.to_thread`` work cannot be force-killed, but bounding the awaited
    result keeps ``GET /api/network`` responsive and lets faster sources render.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("⚠️ %s read timed out after %.0f s", label, timeout_s)
        return fallback


async def fetch_network_state(include_speedtest: bool = False) -> NetworkState:
    """One snapshot: internet health + AP + router + device inventory + alerts.

    The three sources are independent, so they run concurrently. A speed test is
    opt-in (it takes ~10-15 s and saturates the link).
    """
    internet_timeout = _INTERNET_TIMEOUT_S if include_speedtest else _INTERNET_FAST_TIMEOUT_S
    internet, (ap, devices), (router, leases), wifi = await asyncio.gather(
        _with_timeout(
            "internet health",
            fetch_internet_health(include_speedtest=include_speedtest),
            internet_timeout,
            InternetHealth(online=False),
        ),
        _with_timeout(
            "access-point",
            fetch_access_point(),
            _ACCESS_POINT_TIMEOUT_S,
            (AccessPointHealth(reachable=False, error="read timed out"), []),
        ),
        _with_timeout(
            "router",
            fetch_router(),
            _ROUTER_TIMEOUT_S,
            (RouterHealth(reachable=False, error="read timed out"), []),
        ),
        _with_timeout(
            "Wi-Fi diagnostics",
            fetch_wifi_diagnostics(),
            _WIFI_TIMEOUT_S,
            WifiDiagnostics(available=False, error="read timed out"),
        ),
    )
    # When the AP is unreachable and NETWORK_AP_MAC is configured, attempt
    # rediscovery via the router's lease table (already fetched above). This
    # handles the common case where the AP received a new DHCP address: we look
    # up the stable MAC in the lease table, verify the candidate with a short
    # pynetgear login, and update the in-memory runtime host on success.
    if not ap.reachable and leases:
        ap_mac = _ap_mac()
        if ap_mac:
            candidate = _rediscover_ap_host(ap_mac, leases)
            if candidate:
                logger.info(
                    "ℹ️ AP unreachable at configured host; trying discovered IP %s (MAC %s)",
                    candidate, ap_mac,
                )
                try:
                    ap, devices = await asyncio.wait_for(
                        asyncio.to_thread(_fetch_ap_sync, candidate),
                        timeout=_ACCESS_POINT_REDISCOVER_TIMEOUT_S,
                    )
                    if ap.reachable:
                        logger.info("✅ AP rediscovered at %s", candidate)
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.warning("⚠️ AP rediscovery probe at %s failed: %s", candidate, exc)

    # Fold the router DHCP hostnames into the AP inventory (issue #169). A failed
    # or empty router read leaves `leases` empty, so the AP list passes through.
    devices = _merge_router_leases(devices, leases)
    alerts = _derive_alerts(internet, ap, router, devices)
    return NetworkState(
        internet=internet,
        access_point=ap,
        router=router,
        wifi=wifi,
        devices=tuple(devices),
        alerts=tuple(alerts),
    )
