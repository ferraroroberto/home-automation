"""RISCO Cloud security API over ``src.risco_client``.

``GET /api/security`` returns the live alarm snapshot, ``GET
/api/security/events`` returns recent events, and the POST endpoints run the
confirmed one-tap actions. The separate native Partial action remains disabled
until its group mapping is known; the backend rejects unsupported actions
instead of guessing.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.risco_client import (
    ACTIONS,
    RiscoCommandError,
    RiscoConfigError,
    control_system,
    fetch_events,
    fetch_security_state,
    set_zone_bypass,
)
from src.security_display_names import (
    load_security_display_names,
    set_security_display_name,
)
from src.security_hidden import load_hidden_zone_ids, set_zone_hidden

logger = logging.getLogger(__name__)

router = APIRouter()


def _state_payload(state: object) -> Dict[str, Any]:
    """Serialise a ``SecurityState`` and merge per-detector display-name overrides.

    Detectors carry RISCO names like ``"1"``/``"2"``; the override map (keyed by
    the zone id as a string) is layered on as ``display_name`` per zone, mirroring
    how the units/plugs routers surface custom labels (issue #84).
    """
    payload = asdict(state)  # type: ignore[arg-type]
    overrides = load_security_display_names()
    hidden = load_hidden_zone_ids()
    for zone in payload.get("zones") or []:
        zone_id = str(zone.get("id"))
        zone["display_name"] = overrides.get(zone_id) or None
        # Whether the user has parked this detector out of the default list
        # (issue #104). The UI still renders it when "show hidden" is on.
        zone["hidden"] = zone_id in hidden
    return payload


def _events_payload(events: List[object]) -> Dict[str, Any]:
    return {"events": [asdict(event) for event in events]}


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RiscoConfigError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, RiscoCommandError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("Failed to call RISCO Cloud: %s", exc)
    return HTTPException(status_code=502, detail=f"failed to call RISCO Cloud: {exc}")


@router.get("/api/security")
async def get_security() -> Dict[str, Any]:
    try:
        return _state_payload(await fetch_security_state())
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.get("/api/security/events")
async def get_security_events(count: int = 50) -> Dict[str, Any]:
    safe_count = max(1, min(int(count or 50), 100))
    try:
        return _events_payload(await fetch_events(count=safe_count))
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.post("/api/security/{action}")
async def post_security_action(action: str) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action '{action}'")
    try:
        return _state_payload(await control_system(action))
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@router.post("/api/security/zones/{zone_id}/bypass")
async def post_zone_bypass(zone_id: int, request: Request) -> Dict[str, Any]:
    bypass = await _bool_field(request, "bypass")
    try:
        return _state_payload(await set_zone_bypass(zone_id, bypass))
    except (RiscoConfigError, RiscoCommandError) as exc:
        raise _http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


class DisplayNamePayload(BaseModel):
    display_name: str


@router.put("/api/security/zones/{zone_id}/display_name")
async def update_zone_display_name(
    zone_id: int, payload: DisplayNamePayload
) -> Dict[str, Any]:
    name = payload.display_name.strip()
    try:
        set_security_display_name(str(zone_id), name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save detector name for %s: %s", zone_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save display name: {exc}")
    return {"zone_id": zone_id, "display_name": name or None}


class HiddenPayload(BaseModel):
    hidden: bool


@router.put("/api/security/zones/{zone_id}/hidden")
async def update_zone_hidden(
    zone_id: int, payload: HiddenPayload
) -> Dict[str, Any]:
    try:
        set_zone_hidden(str(zone_id), payload.hidden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save detector hidden flag for %s: %s", zone_id, exc)
        raise HTTPException(status_code=500, detail=f"failed to save hidden flag: {exc}")
    return {"zone_id": zone_id, "hidden": payload.hidden}


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
