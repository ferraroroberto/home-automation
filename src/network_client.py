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
import hashlib
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

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
    source: str  # which device reported it: "ap" | "router"

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
    """Vodafone ZXHN F6600P reachability + login state.

    WAN/internet detail is the issue #125 follow-up (ZTE data-read token scheme);
    today this proves the headless login works end to end.
    """

    reachable: bool
    authenticated: bool = False
    model: str = "ZXHN F6600P"
    error: Optional[str] = None


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
class NetworkState:
    """Everything the Network view needs in one snapshot."""

    internet: InternetHealth
    access_point: AccessPointHealth
    router: RouterHealth
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
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=count * timeout_s + 5
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


def _fetch_router_sync() -> RouterHealth:
    host, user, pwd = _router_creds()
    try:
        client = RouterClient(host, user, pwd)
    except NetworkConfigError:
        raise
    try:
        authed = client.login()
    except NetworkCommandError as exc:
        return RouterHealth(reachable=True, authenticated=False, error=str(exc))
    except requests.RequestException as exc:
        return RouterHealth(reachable=False, error=str(exc))
    return RouterHealth(reachable=True, authenticated=authed)


async def fetch_router() -> RouterHealth:
    """Async wrapper: prove the router login works headlessly."""
    try:
        return await asyncio.to_thread(_fetch_router_sync)
    except NetworkConfigError:
        raise
    except Exception as exc:
        logger.warning("⚠️ router read failed: %s", exc)
        return RouterHealth(reachable=False, error=str(exc))


def reboot_router() -> None:
    """Reboot the router. Not wired yet - issue #125 follow-up."""
    raise NotImplementedError(
        "router reboot needs the ZTE session-token POST scheme - issue #125 "
        "follow-up (see docs/network-spike.md). The AP reboot is reboot_access_point()."
    )


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


async def fetch_network_state(include_speedtest: bool = False) -> NetworkState:
    """One snapshot: internet health + AP + router + device inventory + alerts.

    The three sources are independent, so they run concurrently. A speed test is
    opt-in (it takes ~10-15 s and saturates the link).
    """
    internet, (ap, devices), router = await asyncio.gather(
        fetch_internet_health(include_speedtest=include_speedtest),
        fetch_access_point(),
        fetch_router(),
    )
    alerts = _derive_alerts(internet, ap, router, devices)
    return NetworkState(
        internet=internet,
        access_point=ap,
        router=router,
        devices=tuple(devices),
        alerts=tuple(alerts),
    )
