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
from dataclasses import dataclass, replace
from typing import Mapping, Optional

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
_ROUTER_TIMEOUT_S = 18.0
_WIFI_TIMEOUT_S = 6.0


class NetworkConfigError(RuntimeError):
    """Required NETWORK_* environment variables are missing."""


class NetworkCommandError(RuntimeError):
    """A device rejected a command or returned an unusable response."""


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
def _fetch_ap_sync() -> tuple[AccessPointHealth, list[NetDevice]]:
    """Blocking pynetgear read: AP identity + the full attached-device list."""
    from pynetgear import Netgear

    host, user, pwd = _ap_creds()
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


def reboot_access_point() -> None:
    """Reboot the NETGEAR R9000 (proven working via pynetgear)."""
    from pynetgear import Netgear

    host, user, pwd = _ap_creds()
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
