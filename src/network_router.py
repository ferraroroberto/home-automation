r"""
Vodafone ZXHN F6600P (ZTE) router source (issue #197 split).
============================================================
The second device feeding :class:`NetworkState`: the ZTE router reached over its
SHA256 challenge-response web login. Owns the headless login, the authenticated
WAN/DHCP-lease reads, the static DHCP-binding read/write-back (the integrity-
checked ``commConf.IntegCheck`` POST scheme), the lease→AP-inventory merge, and
``reboot_router()``.

Credentials come from ``.env`` (loopback LAN, never committed)::

    NETWORK_ROUTER_HOST / NETWORK_ROUTER_USERNAME / NETWORK_ROUTER_PASSWORD

Extracted verbatim from ``network_client``; the orchestrator imports the
``RouterClient`` protocol surface, the fetch/reboot wrappers, the parsing helpers
and ``_add_bindings_on_client`` from here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import re
from dataclasses import replace
from typing import Mapping, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from src.network_types import (
    DHCP_BIND_MAX,
    DhcpBindingTableFull,
    NetDevice,
    NetworkCommandError,
    NetworkConfigError,
    RouterHealth,
    _normalise_mac,
    _require,
)

logger = logging.getLogger(__name__)


def _router_creds() -> tuple[str, str, str]:
    load_dotenv(override=True)
    return (
        _require("NETWORK_ROUTER_HOST"),
        _require("NETWORK_ROUTER_USERNAME"),
        _require("NETWORK_ROUTER_PASSWORD"),
    )


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
