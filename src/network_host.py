r"""
Host-side network probes — internet health + Wi-Fi diagnostics (issue #197 split).
================================================================================
The third source feeding :class:`NetworkState` (alongside the NETGEAR AP in
``network_ap`` and the ZTE router in ``network_router``): everything measured on
the server PC itself, independent of either device's API.

* **Internet health** — OS ``ping`` (latency + packet loss) to an external
  anchor, plus an opt-in ``speedtest-cli`` throughput run.
* **Wi-Fi diagnostics** — the host WLAN adapter's own view via ``netsh wlan``
  (Windows only): visible BSSIDs, the connected radio, and a channel-cleanliness
  scorer that recommends least-crowded channels.

Extracted verbatim from ``network_client``; the orchestrator imports
``fetch_internet_health`` / ``fetch_wifi_diagnostics`` from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Mapping, Optional

from dotenv import load_dotenv

from src.network_types import (
    InternetHealth,
    WifiBssid,
    WifiChannelInsight,
    WifiChannelScore,
    WifiDiagnostics,
    _normalise_mac,
)

logger = logging.getLogger(__name__)

# External anchor for the "is the internet actually up" probe (Cloudflare DNS).
_EXTERNAL_PROBE_HOST = "1.1.1.1"


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
