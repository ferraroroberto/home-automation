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

    # ---- authenticated data read + reboot (issue #129 Phase 3) ------------- #
    _WAN_FEED = (
        "/?_type=menuData&_tag=wan_internetstatus_lua.lua&TypeUplink=2&pageType=1"
    )
    _REBOOT_FEED = "/?_type=menuData&_tag=devmgr_restartmgr_lua.lua"

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


def _fetch_router_sync() -> RouterHealth:
    host, user, pwd = _router_creds()
    client = RouterClient(host, user, pwd)
    try:
        authed = client.login()
    except NetworkCommandError as exc:
        return RouterHealth(reachable=True, authenticated=False, error=str(exc))
    except requests.RequestException as exc:
        return RouterHealth(reachable=False, error=str(exc))
    if not authed:
        return RouterHealth(reachable=True, authenticated=False)
    # Authenticated → layer on the WAN/internet status (best-effort: a read
    # failure leaves the WAN fields None rather than dropping the login signal).
    try:
        wan = client.read_wan()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ router WAN read failed: %s", exc)
        return RouterHealth(reachable=True, authenticated=True)
    return RouterHealth(
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
