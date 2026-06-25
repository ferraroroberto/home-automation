"""Home-network (LAN) API over ``src.network_client`` + ``src.network_history``.

``GET /api/network`` returns one :class:`NetworkState` snapshot — host-side
internet health, the NETGEAR access-point health + full attached-device
inventory, the Vodafone router reachability/login + WAN status, and the derived
network-quality alerts. Reboots: ``POST /api/network/access-point/reboot`` and
``POST /api/network/router/reboot`` (both confirm-gated client-side).

Phase 4 layers a tiny per-MAC history (:mod:`src.network_history`) on top: each
read records the currently-seen, non-randomised devices, then the response
carries each device's ``online`` state, ``first_seen`` / ``last_seen`` /
``times_seen``, the ``important`` flag, and an ``is_new`` badge — plus
synthesised **offline** rows for known devices absent from this read. Two extra
alerts fall out of that: a never-before-seen device joining, and an important
device dropping offline. ``POST /api/network/devices/{mac}/important`` toggles
the flag.

Partial data is normal and returned with 200: an unreachable AP or router is
reported as ``reachable=false`` on its card, not a 500 — only an unexpected
failure of the whole read surfaces as a 502. The opt-in throughput test
(``?speedtest=1``) takes ~13 s and saturates the link, so it is a deliberate,
separate call the 15 s poll never triggers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Set

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.dhcp_plan import (
    Assignment,
    DhcpPlan,
    build_plan,
    device_inputs_from_inventory,
    load_dhcp_plan_config,
)
from src.network_client import (
    NetDevice,
    NetworkCommandError,
    NetworkConfigError,
    NetworkState,
    WifiBssid,
    WifiChannelInsight,
    WifiChannelScore,
    WifiDiagnostics,
    fetch_network_state,
    reboot_access_point,
    reboot_router,
)
from src.network_display_names import (
    load_network_display_names,
    normalize_mac,
    set_network_display_name,
)
from src.network_hidden import (
    load_hidden_device_macs,
    load_hidden_wifi_ids,
    normalize_wifi_id,
    set_device_hidden,
    set_wifi_hidden,
)
from src.network_history import (
    is_new,
    record_and_snapshot,
    set_important,
)
from src.network_oui import category_for_device, is_randomized_mac, vendor_for_mac
from src.network_wifi_display_names import (
    load_network_wifi_display_names,
    set_network_wifi_display_name,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _device_label(mac: str, name: str | None, vendor: str | None, overrides: Mapping[str, str]) -> str:
    """Identity precedence for an alert/synthesised row: label → vendor → name → MAC.

    Mirrors ``deviceLabel`` in ``network.js`` so an alert names a device the same
    way the list does.
    """
    return overrides.get(mac) or vendor or name or mac


def _device_dict(
    d: NetDevice,
    overrides: Mapping[str, str],
    hidden_macs: Set[str],
    known: Mapping[str, Mapping[str, Any]],
    now: int,
) -> Dict[str, Any]:
    """Flatten one live (online) attached client for the band-grouped list.

    ``is_wireless`` is sent so the client doesn't re-derive the wired/wireless
    split. Identity (Phase 2) is layered on at render time: the custom
    ``display_name`` override (keyed by normalised MAC), the OUI ``vendor``, a
    coarse ``category`` glyph, and ``randomized`` for a locally-administered MAC.
    History (Phase 4) adds ``online`` (always true here), ``first_seen`` /
    ``last_seen`` / ``times_seen``, the ``important`` flag, and an ``is_new``
    badge. Randomised MACs are never recorded, so they carry no history.
    """
    mac_key = normalize_mac(d.mac or "")
    vendor = vendor_for_mac(d.mac or "")
    rec = known.get(mac_key)
    return {
        "mac": d.mac,
        "ip": d.ip,
        "name": d.name,
        "conn_type": d.conn_type,
        "signal": d.signal,
        "link_rate": d.link_rate,
        "ssid": d.ssid,
        "source": d.source,
        "is_wireless": d.is_wireless,
        "display_name": overrides.get(mac_key) or None,
        "vendor": vendor,
        "category": category_for_device(d.name, vendor, d.conn_type),
        "randomized": is_randomized_mac(d.mac or ""),
        "online": True,
        "first_seen": rec["first_seen"] if rec else None,
        "last_seen": rec["last_seen"] if rec else None,
        "times_seen": rec["times_seen"] if rec else None,
        "important": bool(rec["important"]) if rec else False,
        "hidden": mac_key in hidden_macs,
        # ``is_new`` is the 24 h "recently appeared" badge (persists across polls);
        # ``new_macs`` (this exact cycle) drives the one-shot alert instead.
        "is_new": is_new(rec, now) if rec else False,
    }


def _offline_device_dict(
    mac: str,
    rec: Mapping[str, Any],
    overrides: Mapping[str, str],
    hidden_macs: Set[str],
) -> Dict[str, Any]:
    """Synthesise a row for a known device absent from the current AP read.

    Carries the last-known IP/name + the history fields so the list can dim it
    and show "last seen Xh ago"; ``conn_type``/``signal`` are null (we no longer
    observe it). Randomised MACs are never recorded, so an offline row never
    represents a rotating address.
    """
    vendor = vendor_for_mac(mac)
    return {
        "mac": mac,
        "ip": rec.get("last_ip"),
        "name": rec.get("last_name"),
        "conn_type": None,
        "signal": None,
        "link_rate": None,
        "ssid": None,
        "source": "history",
        "is_wireless": False,
        "display_name": overrides.get(mac) or None,
        "vendor": vendor,
        "category": category_for_device(rec.get("last_name"), vendor, None),
        "randomized": False,
        "online": False,
        "first_seen": rec.get("first_seen"),
        "last_seen": rec.get("last_seen"),
        "times_seen": rec.get("times_seen"),
        "important": bool(rec.get("important")),
        "hidden": mac in hidden_macs,
        "is_new": False,
    }


def _wifi_bssid_dict(
    b: WifiBssid,
    wifi_overrides: Mapping[str, str],
    hidden_wifi_ids: Set[str],
) -> Dict[str, Any]:
    """Serialise one host-visible BSSID for the Wi-Fi diagnostics card."""
    wifi_id = normalize_wifi_id(b.bssid, b.ssid)
    return {
        "wifi_id": wifi_id,
        "ssid": b.ssid,
        "original_name": b.ssid,
        "bssid": b.bssid,
        "display_name": wifi_overrides.get(wifi_id) or None,
        "hidden": wifi_id in hidden_wifi_ids,
        "signal": b.signal,
        "rssi_dbm": b.rssi_dbm,
        "channel": b.channel,
        "band": b.band,
        "radio_type": b.radio_type,
        "authentication": b.authentication,
        "encryption": b.encryption,
        "connected": b.connected,
        "channel_width_mhz": b.channel_width_mhz,
    }


def _wifi_channel_score_dict(score: WifiChannelScore) -> Dict[str, Any]:
    return {
        "channel": score.channel,
        "score": score.score,
        "visible_radios": score.visible_radios,
        "strongest_signal": score.strongest_signal,
        "strongest_ssid": score.strongest_ssid,
    }


def _wifi_channel_insight_dict(insight: WifiChannelInsight) -> Dict[str, Any]:
    return {
        "band": insight.band,
        "source": insight.source,
        "recommended_channel": insight.recommended_channel,
        "recommended_width_mhz": insight.recommended_width_mhz,
        "coordinated_channels": list(insight.coordinated_channels),
        "candidate_scores": [
            _wifi_channel_score_dict(score) for score in insight.candidate_scores
        ],
        "rationale": list(insight.rationale),
        "apply_supported": insight.apply_supported,
    }


def _wifi_dict(
    w: WifiDiagnostics,
    wifi_overrides: Mapping[str, str],
    hidden_wifi_ids: Set[str],
) -> Dict[str, Any]:
    """Flatten best-effort host-side Wi-Fi diagnostics."""
    return {
        "available": w.available,
        "interface_name": w.interface_name,
        "adapter_description": w.adapter_description,
        "current_ssid": w.current_ssid,
        "current_bssid": w.current_bssid,
        "current_signal": w.current_signal,
        "current_channel": w.current_channel,
        "current_band": w.current_band,
        "current_radio_type": w.current_radio_type,
        "bssids": [_wifi_bssid_dict(b, wifi_overrides, hidden_wifi_ids) for b in w.bssids],
        "recommendations": list(w.recommendations),
        "insights": [_wifi_channel_insight_dict(insight) for insight in w.insights],
        "error": w.error,
    }


def _history_alerts(
    known: Mapping[str, Mapping[str, Any]],
    online_macs: Set[str],
    new_macs: Set[str],
    overrides: Mapping[str, str],
) -> List[str]:
    """Phase-4 alerts derived from the history: new device + important offline."""
    alerts: List[str] = []
    for mac in sorted(new_macs):
        rec = known.get(mac, {})
        label = _device_label(mac, rec.get("last_name"), vendor_for_mac(mac), overrides)
        alerts.append(f"New device joined the network: {label}.")
    for mac in sorted(known):
        rec = known[mac]
        if rec.get("important") and mac not in online_macs:
            label = _device_label(mac, rec.get("last_name"), vendor_for_mac(mac), overrides)
            alerts.append(f"Important device offline: {label}.")
    return alerts


def _network_dict(
    s: NetworkState,
    overrides: Mapping[str, str],
    hidden_macs: Set[str],
    wifi_overrides: Mapping[str, str],
    hidden_wifi_ids: Set[str],
    known: Mapping[str, Mapping[str, Any]],
    new_macs: Set[str],
    now: int,
) -> Dict[str, Any]:
    """Flatten a :class:`NetworkState` (+ history) into a JSON-serialisable dict."""
    net = s.internet
    ap = s.access_point
    r = s.router

    online_macs = {normalize_mac(d.mac or "") for d in s.devices if d.mac}
    devices = [_device_dict(d, overrides, hidden_macs, known, now) for d in s.devices]
    # Append offline rows for known devices not in the current read (Phase 4).
    for mac in sorted(known):
        if mac not in online_macs:
            devices.append(_offline_device_dict(mac, known[mac], overrides, hidden_macs))

    alerts = list(s.alerts) + _history_alerts(known, online_macs, new_macs, overrides)

    return {
        "internet": {
            "online": net.online,
            "gateway_ms": net.gateway_ms,
            "external_ms": net.external_ms,
            "packet_loss_pct": net.packet_loss_pct,
            "download_mbps": net.download_mbps,
            "upload_mbps": net.upload_mbps,
            "speedtest_server": net.speedtest_server,
        },
        "access_point": {
            "reachable": ap.reachable,
            "model": ap.model,
            "firmware": ap.firmware,
            "mode": ap.mode,
            "device_count": ap.device_count,
            "error": ap.error,
        },
        "router": {
            "reachable": r.reachable,
            "authenticated": r.authenticated,
            "model": r.model,
            "error": r.error,
            # WAN/internet status from the authenticated ZTE read (Phase 3);
            # null when the router is unreachable / login failed / read rejected.
            "wan_online": r.wan_online,
            "public_ip": r.public_ip,
            "gateway": r.gateway,
            "dns": r.dns,
            "connection_name": r.connection_name,
            "uptime_s": r.uptime_s,
            "addressing": r.addressing,
        },
        "wifi": _wifi_dict(s.wifi, wifi_overrides, hidden_wifi_ids),
        "devices": devices,
        "alerts": alerts,
    }


@router.get("/api/network")
async def get_network(
    speedtest: bool = Query(False, description="run an opt-in throughput test (~13 s)"),
) -> Dict[str, Any]:
    try:
        state = await fetch_network_state(include_speedtest=speedtest)
    except NetworkConfigError as exc:
        # Missing NETWORK_* env — surface the guidance, don't 500.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read network state: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read network: {exc}")

    overrides = load_network_display_names()
    hidden_macs = load_hidden_device_macs()
    wifi_overrides = load_network_wifi_display_names()
    hidden_wifi_ids = load_hidden_wifi_ids()
    now = int(time.time())
    # Record the live, non-randomised devices and get back the new-this-cycle
    # MACs + the full registry snapshot. Best-effort: a history failure must not
    # break the live read, so fall back to no history rather than 502.
    seen = [
        {"mac": normalize_mac(d.mac), "ip": d.ip, "name": d.name}
        for d in state.devices
        if d.mac and not is_randomized_mac(d.mac)
    ]
    try:
        new_list, known = await asyncio.to_thread(record_and_snapshot, seen, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  network history update failed: %s", exc)
        new_list, known = [], {}
    return _network_dict(
        state,
        overrides,
        hidden_macs,
        wifi_overrides,
        hidden_wifi_ids,
        known,
        set(new_list),
        now,
    )


def _assignment_dict(a: Assignment) -> Dict[str, Any]:
    return {
        "mac": a.mac,
        "label": a.label,
        "category": a.category,
        "current_ip": a.current_ip,
        "planned_ip": a.planned_ip,
        "randomized": a.randomized,
    }


def _dhcp_plan_dict(plan: DhcpPlan) -> Dict[str, Any]:
    """Serialise a :class:`DhcpPlan` for the read-only Network-tab section."""
    return {
        "categories": [
            {
                "label": c.label,
                "start": c.start,
                "end": c.end,
                "assignments": [_assignment_dict(a) for a in c.assignments],
            }
            for c in plan.categories
        ],
        "unassigned": [_assignment_dict(a) for a in plan.unassigned],
        "warnings": list(plan.warnings),
    }


@router.get("/api/network/dhcp-plan")
async def get_dhcp_plan() -> Dict[str, Any]:
    """Compute the ordered DHCP reservation plan (issue #170) — **read-only**.

    Reads the live inventory, classifies each device into a category range from
    ``config/dhcp_plan.json``, and returns the per-category MAC→IP assignment the
    user applies by hand in the router's DHCP Binding form. No router writes
    happen here (binding write-back is phase 2). Mirrors ``get_network``'s error
    handling: 503 on missing ``NETWORK_*``, 502 on an unexpected read failure.
    """
    try:
        state = await fetch_network_state()
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read network for DHCP plan: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read network: {exc}")

    overrides = load_network_display_names()
    config = load_dhcp_plan_config()
    devices = device_inputs_from_inventory(state.devices, overrides)
    plan = build_plan(devices, config)
    return _dhcp_plan_dict(plan)


@router.post("/api/network/access-point/reboot")
async def post_access_point_reboot() -> Dict[str, Any]:
    """Reboot the NETGEAR access point (the UI gates this behind a confirm)."""
    try:
        await asyncio.to_thread(reboot_access_point)
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Access-point reboot failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"reboot failed: {exc}")
    return {"ok": True}


@router.post("/api/network/router/reboot")
async def post_router_reboot() -> Dict[str, Any]:
    """Reboot the Vodafone router (the UI gates this behind a styled confirm).

    Drops all connections for ~5 min; strictly a deliberate user action (Phase 3).
    """
    try:
        await asyncio.to_thread(reboot_router)
    except NetworkConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except NetworkCommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Router reboot failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"reboot failed: {exc}")
    return {"ok": True}


class DisplayNamePayload(BaseModel):
    display_name: str


class ImportantPayload(BaseModel):
    important: bool


class HiddenPayload(BaseModel):
    hidden: bool


class WifiDisplayNamePayload(BaseModel):
    wifi_id: str
    display_name: str


class WifiHiddenPayload(BaseModel):
    wifi_id: str
    hidden: bool


@router.put("/api/network/devices/{mac}/display_name")
async def update_device_display_name(
    mac: str, payload: DisplayNamePayload
) -> Dict[str, Any]:
    """Set or clear a custom label for one attached device, keyed by MAC.

    Most clients report an ``n/a`` hostname, so this is the only way to tell them
    apart in the list (issue #129 Phase 2). Mirrors the detector/plug rename
    endpoints; the store is the MAC-keyed parallel of those display-name stores.
    """
    name = payload.display_name.strip()
    try:
        set_network_display_name(mac, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save device name for %s: %s", mac, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"mac": normalize_mac(mac), "display_name": name or None}


@router.put("/api/network/devices/{mac}/hidden")
async def update_device_hidden(mac: str, payload: HiddenPayload) -> Dict[str, Any]:
    """Persist whether an attached device is hidden from the default list."""
    try:
        set_device_hidden(mac, payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to set hidden for %s: %s", mac, exc)
        raise HTTPException(status_code=500, detail=f"failed to set hidden: {exc}")
    return {"mac": normalize_mac(mac), "hidden": payload.hidden}


@router.post("/api/network/devices/{mac}/important")
async def update_device_important(
    mac: str, payload: ImportantPayload
) -> Dict[str, Any]:
    """Mark/unmark a device important, so it alerts when it drops offline (Phase 4).

    The flag lives in the history registry (not the rename store) and an
    important device is never auto-pruned from it.
    """
    try:
        await asyncio.to_thread(set_important, mac, payload.important)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to set important for %s: %s", mac, exc)
        raise HTTPException(status_code=500, detail=f"failed to set important: {exc}")
    return {"mac": normalize_mac(mac), "important": payload.important}


@router.put("/api/network/wifi/display_name")
async def update_wifi_display_name(payload: WifiDisplayNamePayload) -> Dict[str, Any]:
    """Set or clear a custom label for one host-visible Wi-Fi radio/network."""
    wifi_id = normalize_wifi_id(payload.wifi_id)
    name = payload.display_name.strip()
    try:
        set_network_wifi_display_name(wifi_id, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save Wi-Fi name for %s: %s", wifi_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"wifi_id": wifi_id, "display_name": name or None}


@router.put("/api/network/wifi/hidden")
async def update_wifi_hidden(payload: WifiHiddenPayload) -> Dict[str, Any]:
    """Persist whether a Wi-Fi radio/network is hidden from the default list."""
    wifi_id = normalize_wifi_id(payload.wifi_id)
    try:
        set_wifi_hidden(wifi_id, payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to set Wi-Fi hidden for %s: %s", wifi_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to set hidden: {exc}")
    return {"wifi_id": wifi_id, "hidden": payload.hidden}
