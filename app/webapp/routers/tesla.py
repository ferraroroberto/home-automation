"""Tesla read API: GET-only, served from the local store.

No route here calls Tesla: the sampler (``app/webapp/tesla_sampler.py``) is
the only reader of the car, and these routes read what it stored. There is no
write, wake or command route, and ``tests/api/test_tesla_api.py`` fails if one
is added.

Until the Fleet API app and an owner login exist the store is never opened,
so an unconfigured install creates no database. The client's error text is
not passed on: it can carry request detail, and the card only needs the
state word.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Literal, TypeVar

from fastapi import APIRouter, Query

from app.webapp.tesla_sampler import load_sampler_config
from src.devices import tesla_auth, tesla_history

router = APIRouter()

T = TypeVar("T")


def _configured() -> bool:
    return tesla_auth.is_configured(tesla_auth.load_app_config())


def _read_store(read: Callable[[], T]) -> T:
    """Run one store read, creating the tables first on a fresh database."""
    tesla_history.init_db()
    return read()


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
        latest = _read_store(tesla_history.latest)
        out.update(sample=latest["sample"], state=latest["state"], checked_at=latest["checked_at"])
    return out


@router.get("/api/tesla")
async def get_tesla() -> Dict[str, Any]:
    """Whether the car is set up, its latest stored sample and the last state seen."""
    return await asyncio.to_thread(_status)


@router.get("/api/tesla/sessions")
async def get_tesla_sessions(limit: int = Query(20, ge=1, le=200)) -> Dict[str, Any]:
    """The most recent drives and charging sessions, newest first."""
    if not await asyncio.to_thread(_configured):
        return {"drives": [], "charging": []}
    return await asyncio.to_thread(_read_store, lambda: tesla_history.recent_sessions(limit))


@router.get("/api/tesla/history")
async def get_tesla_history(period: Literal["week", "month"] = "week") -> Dict[str, Any]:
    """Per-week or per-month km and charged kWh, oldest first."""
    if not await asyncio.to_thread(_configured):
        return {"period": period, "buckets": []}
    buckets = await asyncio.to_thread(_read_store, lambda: tesla_history.history(period))
    return {"period": period, "buckets": buckets}
