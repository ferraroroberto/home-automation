"""Unit read + control API over the MELCloud Home core.

``GET /api/units`` returns every air-to-air unit's live state; ``POST
/api/units/{id}`` writes the changed controls and returns the read-back
snapshot so the client can re-render just that card.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from src.melcloud_client import (
    DeviceInfo,
    DeviceNotFoundError,
    MelCloudConfigError,
    fetch_devices,
    set_device_state,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _unit_dict(d: DeviceInfo) -> Dict[str, Any]:
    """Flatten a :class:`DeviceInfo` into a JSON-serialisable dict."""
    return {
        "unit_id": d.unit_id,
        "name": d.name,
        "building": d.building,
        "power": d.power,
        "operation_mode": d.operation_mode,
        "room_temperature": d.room_temperature,
        "set_temperature": d.set_temperature,
        "fan_speed": d.fan_speed,
        "operation_modes": d.operation_modes,
        "fan_speeds": d.fan_speeds,
        "temp_step": d.temp_step,
        # temp_ranges maps mode -> [min, max] (tuple → list for JSON).
        "temp_ranges": {k: list(v) for k, v in d.temp_ranges.items()},
        "vane_vertical": d.vane_vertical,
        "vane_horizontal": d.vane_horizontal,
        "vane_vertical_options": d.vane_vertical_options,
        "vane_horizontal_options": d.vane_horizontal_options,
        "has_vane_vertical": d.has_vane_vertical,
        "has_vane_horizontal": d.has_vane_horizontal,
    }


@router.get("/api/units")
async def list_units() -> Dict[str, Any]:
    try:
        devices = await fetch_devices()
    except MelCloudConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        logger.warning("⚠️  Failed to fetch units: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to fetch units: {exc}")
    return {"units": [_unit_dict(d) for d in devices]}


@router.post("/api/units/{unit_id}")
async def control_unit(unit_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    # Only forward the fields the client actually sent; everything else
    # stays None and is left untouched by the client.
    allowed = (
        "power",
        "operation_mode",
        "set_temperature",
        "fan_speed",
        "vane_vertical_direction",
        "vane_horizontal_direction",
    )
    kwargs = {k: body[k] for k in allowed if k in body}

    try:
        updated = await set_device_state(unit_id, **kwargs)
    except MelCloudConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any API/network error
        logger.warning("⚠️  Failed to control unit %s: %s", unit_id, exc)
        raise HTTPException(status_code=502, detail=f"failed to apply: {exc}")
    return _unit_dict(updated)
