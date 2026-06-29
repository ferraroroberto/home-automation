"""Local Smart Life / Tuya device API over ``src.tuya_client``.

``GET /api/tuya`` lists every captured Tuya device with its live switch state
and current wattage; ``POST /api/tuya/{id}/switch`` and
``POST /api/tuya/{id}/cover`` write on/off and blind controls. Everything here
is LAN-only through the gitignored ``devices.json`` — no Tuya Cloud at runtime.

Listing fans the per-device LAN status reads out in parallel (TinyTuya is
blocking, so each read runs in a worker thread) and catches failures per
device: an offline plug renders as ``reachable=false`` while reachable plugs
stay live and controllable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.webapp.routers._helpers import make_display_name_endpoint
from src.tuya_display_names import load_tuya_display_names, set_tuya_display_name
from src.tuya_hidden import load_hidden_tuya_ids, set_tuya_hidden
from src.tuya_client import (
    TuyaCommandError,
    TuyaConfigError,
    TuyaDeviceInfo,
    TuyaDeviceNotFoundError,
    list_devices,
    read_device_state,
    rescan_addresses,
    set_cover,
    set_switch,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Bound the parallel LAN reads so a large devices.json can't open dozens of
# sockets at once; offline plugs each block ~2s (timeout + one retry).
_READ_CONCURRENCY = 8


def _unique_devices(infos: List[TuyaDeviceInfo]) -> List[TuyaDeviceInfo]:
    """Collapse duplicate ``device_id`` entries, preferring a usable LAN IP.

    ``devices.json`` may hold several rows for one device (e.g. a snapshot
    plus a wizard entry); the UI wants exactly one card per device.
    """
    chosen: Dict[str, TuyaDeviceInfo] = {}
    for info in infos:
        if not info.device_id:
            continue
        current = chosen.get(info.device_id)
        if current is None or (info.has_valid_ip and not current.has_valid_ip):
            chosen[info.device_id] = info
    return list(chosen.values())


def _base_card(
    info: TuyaDeviceInfo,
    overrides: Optional[Dict[str, str]] = None,
    hidden_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """The metadata-only half of a device card (no LAN read needed)."""
    names = overrides or {}
    return {
        "device_id": info.device_id,
        "name": info.name,
        "display_name": names.get(info.device_id) or None,
        "hidden": bool(hidden_ids and info.device_id in hidden_ids),
        "category": info.category,
        "ip": info.ip,
        "mac": info.mac,
        "uuid": info.uuid,
        "sn": info.sn,
        "has_switch": info.switch_dps is not None,
        "has_cover": info.cover_control_dps is not None,
        "metered": bool(info.energy_dps),
        "has_valid_ip": info.has_valid_ip,
        "reachable": False,
        "switch_on": None,
        "power_w": None,
        "current_ma": None,
        "voltage_v": None,
        "energy_kwh": None,
        "error": None,
    }


def _read_one(
    info: TuyaDeviceInfo,
    overrides: Optional[Dict[str, str]] = None,
    hidden_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Blocking LAN read for one device; safe to run in a worker thread.

    Devices without a usable IP or local key are reported unavailable without
    a network attempt (a missing IP would otherwise trigger a slow scan).
    """
    card = _base_card(info, overrides, hidden_ids)
    # Distinct reasons for distinct conditions: a missing local key needs the
    # one-time wizard, a missing IP means the device didn't answer the LAN scan,
    # and a present-but-unreadable IP means it's powered off / off-network.
    if not info.has_local_key:
        card["error"] = "No local key — run `python -m tinytuya wizard` once to capture it."
        return card
    if not info.has_valid_ip:
        card["error"] = (
            "No local IP — didn't answer the LAN scan (powered off or on another network?)."
        )
        return card
    try:
        state = read_device_state(info.device_id)
    except (TuyaCommandError, TuyaConfigError) as exc:
        card["error"] = "Offline — no response on the LAN (powered off?)."
        logger.info("ℹ️ Tuya device %s unreachable: %s", info.device_id, exc)
        return card
    card.update(
        reachable=True,
        switch_on=state.get("switch_on"),
        power_w=state.get("power_w"),
        current_ma=state.get("current_ma"),
        voltage_v=state.get("voltage_v"),
        energy_kwh=state.get("energy_kwh"),
    )
    return card


@router.get("/api/tuya")
async def list_tuya() -> Dict[str, Any]:
    try:
        infos = _unique_devices(list_devices())
    except TuyaConfigError as exc:
        # Missing/empty devices.json — surface the guidance, don't 500.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to list Tuya devices: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to list devices: {exc}")

    overrides = load_tuya_display_names()
    hidden_ids = load_hidden_tuya_ids()
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)

    async def _bounded(info: TuyaDeviceInfo) -> Dict[str, Any]:
        async with semaphore:
            return await asyncio.to_thread(_read_one, info, overrides, hidden_ids)

    cards = await asyncio.gather(*(_bounded(info) for info in infos))
    return {"devices": list(cards)}


@router.post("/api/tuya/refresh")
async def refresh_tuya() -> Dict[str, Any]:
    """Explicit UI refresh path: live LAN rescan, then read back state.

    A plug that took a new DHCP lease has a stale IP in ``devices.json``, so
    merely re-reading the file leaves it offline forever. This runs a TinyTuya
    UDP broadcast scan (no Tuya Cloud, no local keys), reconciles the discovered
    LAN addresses into ``devices.json`` by device id, then retries the per-device
    reads. The scan is gated to this explicit action because a broadcast scan
    takes several seconds — page-load reads stay fast off the stored file.
    """
    try:
        summary = await asyncio.to_thread(rescan_addresses)
    except TuyaConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface scan failure, keep serving
        logger.warning("⚠️  Tuya LAN rescan failed: %s", exc)
        summary = {"found": 0, "updated": [], "addresses": {}, "error": str(exc)}

    body = await list_tuya()
    found = summary.get("found", 0)
    updated = summary.get("updated", [])
    if summary.get("error"):
        detail = f"LAN scan failed ({summary['error']}); showing last-known state."
    elif updated:
        detail = (
            f"LAN scan found {found} device(s); recovered {len(updated)} stale "
            "address(es) and refreshed live state."
        )
    elif found:
        detail = f"LAN scan found {found} device(s); stored addresses already current."
    else:
        detail = (
            "LAN scan found no devices — make sure you're on the home network "
            "and the plugs are powered on."
        )
    body["refresh"] = {"safe": True, "found": found, "updated": updated, "detail": detail}
    return body


@router.post("/api/tuya/{device_id}/switch")
async def control_switch(device_id: str, request: Request) -> Dict[str, Any]:
    on = await _bool_field(request, "on")
    try:
        await asyncio.to_thread(set_switch, device_id, on)
    except TuyaDeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (TuyaCommandError, TuyaConfigError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Read back so the card re-renders from live state, not the requested value.
    try:
        overrides = load_tuya_display_names()
        hidden_ids = load_hidden_tuya_ids()
        info = next(
            (i for i in _unique_devices(list_devices()) if i.device_id == device_id),
            None,
        )
        card = await asyncio.to_thread(_read_one, info, overrides, hidden_ids) if info else None
    except Exception:  # noqa: BLE001 — read-back is best-effort
        card = None
    if card is None:
        return {"device_id": device_id, "reachable": False, "switch_on": on}
    return card


@router.post("/api/tuya/{device_id}/cover")
async def control_cover(device_id: str, request: Request) -> Dict[str, Any]:
    action = await _str_field(request, "action")
    if action not in ("open", "close", "stop"):
        raise HTTPException(status_code=400, detail="action must be open/close/stop")
    try:
        await asyncio.to_thread(set_cover, device_id, action)  # type: ignore[arg-type]
    except TuyaDeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (TuyaCommandError, TuyaConfigError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"device_id": device_id, "reachable": True, "action": action, "ok": True}


make_display_name_endpoint(
    router, "/api/tuya/{item_id}/display_name", "device_id", set_tuya_display_name
)


class HiddenPayload(BaseModel):
    hidden: bool


@router.put("/api/tuya/{device_id}/hidden")
async def update_hidden(device_id: str, payload: HiddenPayload) -> Dict[str, Any]:
    """Mark or unmark a Tuya device as hidden (local override, gitignored)."""
    try:
        set_tuya_hidden(device_id, payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save hidden state for %s: %s", device_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save hidden state: {exc}")
    return {"device_id": device_id, "hidden": payload.hidden}


# --------------------------------------------------------------- body helpers
async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


async def _bool_field(request: Request, name: str) -> bool:
    body = await _json_body(request)
    value = body.get(name)
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"'{name}' must be a boolean")
    return value


async def _str_field(request: Request, name: str) -> Optional[str]:
    body = await _json_body(request)
    value = body.get(name)
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"'{name}' must be a string")
    return value
