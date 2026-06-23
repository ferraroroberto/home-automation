"""Home-network (LAN) API over ``src.network_client``.

``GET /api/network`` returns one :class:`NetworkState` snapshot — host-side
internet health, the NETGEAR access-point health + full attached-device
inventory, the Vodafone router reachability/login signal, and the derived
network-quality alerts. ``POST /api/network/access-point/reboot`` reboots the
R9000 (confirm-gated client-side).

Partial data is normal and returned with 200: an unreachable AP or router is
reported as ``reachable=false`` on its card, not a 500 — only an unexpected
failure of the whole read surfaces as a 502. The opt-in throughput test
(``?speedtest=1``) takes ~13 s and saturates the link, so it is a deliberate,
separate call the 15 s poll never triggers.

The ZTE router data-read (WAN status) and ``reboot_router()`` are the issue
#129 Phase-3 follow-up; there is intentionally no router-reboot route yet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Mapping

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.network_client import (
    NetDevice,
    NetworkCommandError,
    NetworkConfigError,
    NetworkState,
    fetch_network_state,
    reboot_access_point,
)
from src.network_display_names import (
    load_network_display_names,
    normalize_mac,
    set_network_display_name,
)
from src.network_oui import category_for_device, is_randomized_mac, vendor_for_mac

logger = logging.getLogger(__name__)

router = APIRouter()


def _device_dict(d: NetDevice, overrides: Mapping[str, str]) -> Dict[str, Any]:
    """Flatten one attached client for the band-grouped list.

    ``is_wireless`` is sent so the client doesn't re-derive the wired/wireless
    split. Identity is layered on at render time (issue #129 Phase 2): the
    custom ``display_name`` override (keyed by normalised MAC, like the
    units/plugs/detectors stores), the OUI ``vendor``, a coarse ``category`` for
    the row glyph, and ``randomized`` for a locally-administered (rotating) MAC.
    """
    vendor = vendor_for_mac(d.mac or "")
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
        "display_name": overrides.get(normalize_mac(d.mac or "")) or None,
        "vendor": vendor,
        "category": category_for_device(d.name, vendor, d.conn_type),
        "randomized": is_randomized_mac(d.mac or ""),
    }


def _network_dict(s: NetworkState, overrides: Mapping[str, str]) -> Dict[str, Any]:
    """Flatten a :class:`NetworkState` into a JSON-serialisable dict."""
    net = s.internet
    ap = s.access_point
    r = s.router
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
        },
        "devices": [_device_dict(d, overrides) for d in s.devices],
        "alerts": list(s.alerts),
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
    return _network_dict(state, load_network_display_names())


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


class DisplayNamePayload(BaseModel):
    display_name: str


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
