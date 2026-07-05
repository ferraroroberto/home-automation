"""Auto-bypass-after-N-repeats detector rules CRUD over ``src.security_override``.

Split out of ``routers/security.py`` (issue #346) — same rationale as
``security_schedules.py``: the per-detector override rule list (issue #341)
is fully self-contained and shares no state with the live RISCO read/write
path that stays in ``security.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import _json_body
from src.security_override import load_overrides, set_overrides

logger = logging.getLogger(__name__)

router = APIRouter()


def _overrides_payload(entries: List[object]) -> Dict[str, Any]:
    active = [e for e in entries if getattr(e, "enabled", False)]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [asdict(e) for e in entries],
    }


@router.get("/api/security/overrides")
async def get_overrides() -> Dict[str, Any]:
    """Return the "auto-bypass after N repeats this session" detector rules (issue #341)."""
    try:
        return _overrides_payload(load_overrides())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load alarm overrides: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load overrides: {exc}")


@router.put("/api/security/overrides")
async def update_overrides(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _overrides_payload(set_overrides(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save alarm overrides: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save overrides: {exc}")
