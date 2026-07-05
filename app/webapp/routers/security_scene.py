"""Alarm scene-capture pairings CRUD over ``src.alarm_scene_config``.

Split out of ``routers/security.py`` (issue #346) — same rationale as
``security_schedules.py``: the detector→camera+preset pairing list (issue
#162) is fully self-contained and shares no state with the live RISCO
read/write path that stays in ``security.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import _json_body
from src.alarm_scene_config import load_scene_pairings, set_scene_pairings

logger = logging.getLogger(__name__)

router = APIRouter()


def _pairings_payload(pairings: List[object]) -> Dict[str, Any]:
    active = [p for p in pairings if getattr(p, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(p) for p in pairings],
    }


@router.get("/api/security/scene-pairings")
async def get_scene_pairings() -> Dict[str, Any]:
    """Return the detector→camera+preset alarm-scene capture pairings (issue #162)."""
    try:
        return _pairings_payload(load_scene_pairings())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load scene pairings: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load scene pairings: {exc}")


@router.put("/api/security/scene-pairings")
async def update_scene_pairings(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _pairings_payload(set_scene_pairings(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save scene pairings: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save scene pairings: {exc}")
