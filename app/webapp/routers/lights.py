"""Elgato lights API over ``src.elgato_client``."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.webapp.read_snapshot import ReadSnapshot
from app.webapp.routers._helpers import make_http_error_mapper
from src.elgato_client import (
    ElgatoCommandError,
    ElgatoConfigError,
    ElgatoDiscoveryError,
    ElgatoLight,
    fetch_lights,
    set_light_state,
)
from src.elgato_display_names import (
    elgato_display_key,
    load_elgato_display_names,
    set_elgato_display_name,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The lights, answered from memory: every read re-ran a 3 s mDNS sweep,
# which each Devices-tab load waited out. Serve-stale like the LAN snapshot:
# once a value exists a read answers at once and one shared refresh runs
# behind it. No tick (demand_window_s=0), so a refresh only ever follows a read
# and the lights see no more sweeps than the 15 s card poll made before. Past
# stale_after_s the answer says ``stale``. Display names are read per request,
# so a rename shows at once.
LIGHTS_SNAPSHOT: ReadSnapshot[List[ElgatoLight]] = ReadSnapshot(
    "lights",
    lambda: fetch_lights(),  # looked up per call, so tests can patch it
    max_age_s=12.0,
    tick_s=15.0,
    demand_window_s=0,
    stale_after_s=60.0,
)


def _light_dict(light: Any, display_names: Dict[str, str]) -> Dict[str, Any]:
    data = asdict(light)
    key = elgato_display_key(light.light_id, light.mac_address)
    data["display_key"] = key
    data["display_name"] = display_names.get(key) or display_names.get(light.light_id)
    return data


_http_error = make_http_error_mapper(
    (ElgatoConfigError, ElgatoDiscoveryError), ElgatoCommandError, noun="Elgato lights"
)


def _with_light(held: List[ElgatoLight], light: ElgatoLight) -> List[ElgatoLight]:
    """*held* with *light* swapped in for the one sharing its id."""
    return [light if item.light_id == light.light_id else item for item in held]


@router.get("/api/lights")
async def list_lights() -> Dict[str, Any]:
    try:
        lights, snapshot = await LIGHTS_SNAPSHOT.read()
        display_names = load_elgato_display_names()
        return {
            "lights": [_light_dict(light, display_names) for light in lights],
            "snapshot": snapshot,
        }
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class LightControlPayload(BaseModel):
    on: Optional[bool] = None
    brightness: Optional[int] = None
    temperature: Optional[int] = None
    temperature_k: Optional[int] = None


class LightDisplayNamePayload(BaseModel):
    display_name: str = ""
    display_key: Optional[str] = None


@router.post("/api/lights/refresh")
async def refresh_lights() -> Dict[str, Any]:
    """Explicit UI refresh path for Elgato mDNS/configured endpoints."""
    LIGHTS_SNAPSHOT.invalidate()  # a live sweep now, not the held list
    body = await list_lights()
    body["refresh"] = {
        "safe": True,
        "detail": "Re-ran mDNS/configured-host discovery and retried each light endpoint.",
    }
    return body


@router.post("/api/lights/{light_id}")
async def control_light(light_id: str, payload: LightControlPayload) -> Dict[str, Any]:
    try:
        light = await set_light_state(
            light_id,
            on=payload.on,
            brightness=payload.brightness,
            temperature=payload.temperature,
            temperature_k=payload.temperature_k,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    # The write read the light back; that reading replaces the held one, so the
    # next poll shows it without another sweep.
    LIGHTS_SNAPSHOT.update(lambda held: _with_light(held, light))
    return _light_dict(light, load_elgato_display_names())


@router.put("/api/lights/{light_id}/display_name")
async def set_light_display_name(
    light_id: str, payload: LightDisplayNamePayload
) -> Dict[str, Any]:
    try:
        display_name = payload.display_name.strip()
        key = payload.display_key or light_id
        set_elgato_display_name(key, display_name)
        return {"light_id": light_id, "display_key": key, "display_name": display_name or None}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
