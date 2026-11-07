"""Tesla read API: GET-only, served from the local store.

``GET /api/tesla`` backs the Devices car card: whether the car is set up,
its latest stored sample and the last state seen. ``GET /api/tesla/history``
and ``GET /api/tesla/sessions`` back the card's history sheet: per-week or
per-month figures and the latest drives and charging sessions.

No route here calls Tesla: the sampler (``app/webapp/tesla_sampler.py``) is
the only reader of the car, and this route reads what it stored. There is no
write, wake or command route, and ``tests/api/test_tesla_api.py`` fails if one
is added.

Until the Fleet API app and an owner login exist the store is never opened,
so an unconfigured install creates no database, and every route answers
``configured: false`` with nothing to show. The client's error text is
not passed on: it can carry request detail, and the card only needs the
state word.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Literal

from fastapi import APIRouter, Query

from app.webapp.tesla_sampler import load_sampler_config
from src.devices import tesla_auth, tesla_history

router = APIRouter()


def _configured() -> bool:
    """Whether the car is set up; when it is, the store has its tables."""
    if not tesla_auth.is_configured(tesla_auth.load_app_config()):
        return False
    tesla_history.init_db()  # a fresh database has no tables yet
    return True


def _status() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "configured": _configured(),
        "sampler_enabled": load_sampler_config().enabled,
        "usable_kwh": tesla_history.load_history_config().usable_kwh,
        "sample": None,
        "state": None,
        "checked_at": None,
    }
    if out["configured"]:
        latest = tesla_history.latest()
        out.update(sample=latest["sample"], state=latest["state"], checked_at=latest["checked_at"])
    return out


@router.get("/api/tesla")
async def get_tesla() -> Dict[str, Any]:
    """Whether the car is set up, its latest stored sample and the last state seen."""
    return await asyncio.to_thread(_status)



def _history(period: str) -> Dict[str, Any]:
    configured = _configured()
    return {
        "configured": configured,
        "sampler_enabled": load_sampler_config().enabled,
        "period": period,
        "buckets": tesla_history.history(period) if configured else [],
    }


def _sessions(limit: int) -> Dict[str, Any]:
    if not _configured():
        return {"configured": False, "drives": [], "charging": []}
    return {"configured": True, **tesla_history.recent_sessions(limit)}


@router.get("/api/tesla/history")
async def get_tesla_history(period: Literal["week", "month"] = "week") -> Dict[str, Any]:
    """Km, drive energy and charged energy (home and total) per week or month, oldest first."""
    return await asyncio.to_thread(_history, period)


@router.get("/api/tesla/sessions")
async def get_tesla_sessions(limit: int = Query(10, ge=1, le=100)) -> Dict[str, Any]:
    """The latest drives and charging sessions, newest first."""
    return await asyncio.to_thread(_sessions, limit)
