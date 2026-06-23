"""Elgato lights API over ``src.elgato_client``."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.elgato_client import (
    ElgatoCommandError,
    ElgatoConfigError,
    ElgatoDiscoveryError,
    fetch_lights,
    set_light_state,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ElgatoConfigError, ElgatoDiscoveryError)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ElgatoCommandError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("⚠️ Failed to call Elgato lights: %s", exc)
    return HTTPException(status_code=502, detail=f"failed to call Elgato lights: {exc}")


@router.get("/api/lights")
async def list_lights() -> Dict[str, Any]:
    try:
        return {"lights": [asdict(light) for light in await fetch_lights()]}
    except (ElgatoConfigError, ElgatoDiscoveryError, ElgatoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class LightControlPayload(BaseModel):
    on: Optional[bool] = None
    brightness: Optional[int] = None
    temperature: Optional[int] = None
    temperature_k: Optional[int] = None


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
        return asdict(light)
    except (ElgatoConfigError, ElgatoDiscoveryError, ElgatoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
