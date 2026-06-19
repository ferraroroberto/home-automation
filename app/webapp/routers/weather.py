"""Current-weather API for the header readout.

``GET /api/weather`` returns the home's current temperature + a WMO weather
code, read from **Open-Meteo** (keyless, no account). The home location lives
in a gitignored ``config/location.json`` (see ``src/location_config.py``); the
real coordinates never enter the public repo.

Failure is quiet, never a 500: a missing location config or an unreachable
Open-Meteo returns HTTP 200 with ``available=False`` so the frontend simply
keeps the readout hidden — weather is decorative, not load-bearing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import aiohttp
from fastapi import APIRouter

from src.location_config import load_location_config

logger = logging.getLogger(__name__)

router = APIRouter()

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 8.0


@router.get("/api/weather")
async def get_weather() -> Dict[str, Any]:
    """Current temperature + WMO weather code for the home location.

    Always 200. ``available=False`` (with a ``reason``) when the location is
    not configured or Open-Meteo could not be reached.
    """
    loc = load_location_config()
    if loc is None:
        return {"available": False, "reason": "not_configured"}

    params = {
        "latitude": loc.lat,
        "longitude": loc.lon,
        "current": "temperature_2m,weather_code,is_day",
        "timezone": "auto",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_OPEN_METEO_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001 — weather is decorative, fail quiet
        logger.warning("⚠️ Failed to read weather: %s", exc)
        return {"available": False, "reason": "unreachable"}

    current = data.get("current") or {}
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    if temp is None or code is None:
        return {"available": False, "reason": "no_data"}

    return {
        "available": True,
        "temperature_c": float(temp),
        "weather_code": int(code),
        "is_day": bool(current.get("is_day", 1)),
        "label": loc.label,
    }
