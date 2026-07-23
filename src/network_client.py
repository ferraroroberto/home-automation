r"""
Home-network read + control orchestrator (async, UI-free)
=========================================================
Spike core for issue #125 — the Network view. Mirrors the other domain cores
(``melcloud_client`` / ``sma_client`` / ``risco_client``): no Streamlit, no
FastAPI, just credentials in → a flattened :class:`NetworkState` out, plus the
reboot + DHCP-reservation controls.

As of issue #197 the three independent device/probe surfaces this module used to
inline live in their own files; this module is now the **orchestrator** that
imports them and exposes the unchanged public surface so callers don't move:

* **NETGEAR R9000 access point** → :mod:`src.network_ap` (inventory, AP health,
  ``reboot_access_point``, MAC rediscovery).
* **Vodafone ZXHN F6600P router (ZTE)** → :mod:`src.network_router`
  (``RouterClient`` login + WAN/DHCP reads + binding write-back, ``reboot_router``).
* **Host-side internet + Wi-Fi probes** → :mod:`src.network_host` (ping / packet
  loss / speedtest / ``netsh wlan``).
* **Shared dataclasses, exceptions, leaf helpers** → :mod:`src.network_types`.

What stays here: the aggregate :func:`fetch_network_state`, the AP-rediscovery
fold, the alert derivation, the cross-domain :func:`resolve_ip_by_mac`, and the
confirm-gated DHCP-reservation control entry points (login once, apply on one
session). Credentials still come from ``.env`` (loopback LAN, never committed)::

    NETWORK_AP_HOST / NETWORK_AP_USERNAME / NETWORK_AP_PASSWORD
    NETWORK_AP_MAC (optional) — stable MAC of the AP; enables auto-rediscovery
    NETWORK_ROUTER_HOST / NETWORK_ROUTER_USERNAME / NETWORK_ROUTER_PASSWORD
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Mapping, Optional

import requests

# Shared types + leaf helpers.
from src.network_types import (  # noqa: F401 — re-exported public surface
    DHCP_BIND_MAX,
    AccessPointHealth,
    DhcpBindingTableFull,
    DhcpReservationLost,
    InternetHealth,
    NetDevice,
    NetworkCommandError,
    NetworkConfigError,
    NetworkState,
    RouterHealth,
    WifiBssid,
    WifiChannelInsight,
    WifiChannelScore,
    WifiDiagnostics,
    _normalise_mac,
    _require,
)
# Host-side internet + Wi-Fi probes.
from src.network_host import (  # noqa: F401 — re-exported public surface
    fetch_internet_health,
    fetch_wifi_diagnostics,
    _parse_wifi_interfaces,
    _parse_wifi_networks,
    _wifi_channel_insights,
    _wifi_recommendations,
)
# NETGEAR access-point source.
from src.network_ap import (  # noqa: F401 — re-exported public surface
    fetch_access_point,
    reboot_access_point,
    _ap_mac,
    _fetch_ap_sync,
    _rediscover_ap_host,
)
# ZTE router source + DHCP-binding protocol.
from src.network_router import (  # noqa: F401 — re-exported public surface
    RouterClient,
    fetch_router,
    reboot_router,
    _add_bindings_on_client,
    _asy_encode,
    _merge_router_leases,
    _merge_router_wlan_clients,
    _parse_instances,
    _pick_internet_wan,
    _router_creds,
)

logger = logging.getLogger(__name__)

# A wireless client below this signal % is surfaced as a weak-link alert.
_WEAK_SIGNAL_PCT = 40
# Packet loss above this % to the outside world is surfaced as an alert.
_HIGH_LOSS_PCT = 5.0
# Keep the aggregate API under the browser's 30 s request budget even if one
# device API stalls. The individual source tasks still run concurrently.
_INTERNET_TIMEOUT_S = 24.0
_INTERNET_FAST_TIMEOUT_S = 10.0
_ACCESS_POINT_TIMEOUT_S = 12.0
_ACCESS_POINT_REDISCOVER_TIMEOUT_S = 5.0
_ROUTER_TIMEOUT_S = 18.0
_WIFI_TIMEOUT_S = 6.0


# --------------------------------------------------------------------------- #
# Cross-domain helper                                                         #
# --------------------------------------------------------------------------- #
async def resolve_ip_by_mac(mac: str) -> Optional[str]:
    """Best-effort current IP for a device MAC, from the live device inventory.

    Lets other domains (e.g. cameras, issue #190; MAC-pinned config, issue #504)
    self-heal a stale configured IP the way the AP and Tuya plugs already do.
    Returns None when every source fails or the MAC isn't present — callers treat
    that as "no recovery available" and leave the device flagged unreachable.

    Both sources are consulted, because neither is complete on its own: clients
    on the *router's* own radios are invisible to the AP (issue #502), so an AP
    miss is not a miss overall. The AP is tried first as the cheaper read.
    """
    target = _normalise_mac(mac)
    if not target:
        return None
    try:
        _health, devices = await fetch_access_point()
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        logger.info("ℹ️ MAC→IP resolve via AP failed: %s", exc)
        devices = []
    for dev in devices:
        if _normalise_mac(dev.mac) == target and dev.ip:
            return dev.ip
    try:
        _router, leases, wlan_clients = await fetch_router()
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        logger.info("ℹ️ MAC→IP resolve via router failed: %s", exc)
        return None
    # Wireless clients first: they carry the address the device is using now,
    # whereas a lease row can outlive the device's presence.
    for row in (*wlan_clients, *leases):
        if _normalise_mac(row.get("mac")) == target and row.get("ip"):
            return row["ip"]
    return None


# --------------------------------------------------------------------------- #
# DHCP reservation control (issue #176) — confirm-gated, never on a poll       #
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
    internet, (ap, devices), (router, leases, wlan_clients), wifi = await asyncio.gather(
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
            (RouterHealth(reachable=False, error="read timed out"), [], []),
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

    # Fold the router's own wireless clients in first (issue #502) — they are
    # invisible to the AP, so this is the only source for them; then the router
    # hostnames (issue #169). A failed or empty router read leaves both lists
    # empty, so the AP list passes through unchanged.
    devices = _merge_router_wlan_clients(devices, wlan_clients)
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
