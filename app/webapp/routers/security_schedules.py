"""Weekly alarm-schedule CRUD over ``src.security_schedules``.

Split out of ``routers/security.py`` (issue #346) — the same "split a grown
router by self-contained concern" move ``dhcp_plan.py`` made out of
``network.py`` in #328. The weekly schedule list is fully self-contained
(schema + persistence in :mod:`src.security_schedules`) and shares no state
with the live RISCO read/write path that stays in ``security.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import _json_body
from src.security_schedules import load_security_schedules, set_security_schedules

logger = logging.getLogger(__name__)

router = APIRouter()


def _schedule_payload(entries: List[object]) -> Dict[str, Any]:
    active = [entry for entry in entries if getattr(entry, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(entry) for entry in entries],
    }


@router.get("/api/security/schedules")
async def get_security_schedules() -> Dict[str, Any]:
    try:
        return _schedule_payload(load_security_schedules())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load alarm schedules: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load schedules: {exc}")


@router.put("/api/security/schedules")
async def update_security_schedules(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _schedule_payload(set_security_schedules(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save alarm schedules: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save schedules: {exc}")
