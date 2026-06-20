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

from src.tuya_display_names import load_tuya_display_names, set_tuya_display_name
from src.tuya_client import (
    TuyaCommandError,
    TuyaConfigError,
    TuyaDeviceInfo,
    TuyaDeviceNotFoundError,
    list_devices,
    read_device_state,
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
    info: TuyaDeviceInfo, overrides: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """The metadata-only half of a device card (no LAN read needed)."""
    names = overrides or {}
    return {
        "device_id": info.device_id,
        "name": info.name,
        "display_name": names.get(info.device_id) or None,
        "category": info.category,
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
    info: TuyaDeviceInfo, overrides: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Blocking LAN read for one device; safe to run in a worker thread.

    Devices without a usable IP or local key are reported unavailable without
    a network attempt (a missing IP would otherwise trigger a slow scan).
    """
    card = _base_card(info, overrides)
    if not info.has_valid_ip or not info.has_local_key:
        card["error"] = "No local IP — refresh devices.json on the home network."
        return card
    try:
        state = read_device_state(info.device_id)
    except (TuyaCommandError, TuyaConfigError) as exc:
        card["error"] = "Offline — refresh devices.json if this persists."
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
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)

    async def _bounded(info: TuyaDeviceInfo) -> Dict[str, Any]:
        async with semaphore:
            return await asyncio.to_thread(_read_one, info, overrides)

    cards = await asyncio.gather(*(_bounded(info) for info in infos))
    return {"devices": list(cards)}


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
        info = next(
            (i for i in _unique_devices(list_devices()) if i.device_id == device_id),
            None,
        )
        card = await asyncio.to_thread(_read_one, info, overrides) if info else None
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


class DisplayNamePayload(BaseModel):
    display_name: str


@router.put("/api/tuya/{device_id}/display_name")
async def update_display_name(device_id: str, payload: DisplayNamePayload) -> Dict[str, Any]:
    """Set or clear a Tuya device's local display-name override (gitignored)."""
    name = payload.display_name.strip()
    try:
        set_tuya_display_name(device_id, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save display name for %s: %s", device_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"device_id": device_id, "display_name": name or None}


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
