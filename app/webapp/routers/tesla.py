"""Tesla read API: GET-only, served from the local store.

``GET /api/tesla`` backs the Devices car card: whether the car is set up,
its latest stored sample and the last state seen.

No route here calls Tesla: the sampler (``app/webapp/tesla_sampler.py``) is
the only reader of the car, and this route reads what it stored. There is no
write, wake or command route, and ``tests/api/test_tesla_api.py`` fails if one
is added.

Until the Fleet API app and an owner login exist the store is never opened,
so an unconfigured install creates no database. The client's error text is
not passed on: it can carry request detail, and the card only needs the
state word.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter

from app.webapp.tesla_sampler import load_sampler_config
from src.devices import tesla_auth, tesla_history

router = APIRouter()

def _status() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "configured": tesla_auth.is_configured(tesla_auth.load_app_config()),
        "sampler_enabled": load_sampler_config().enabled,
        "usable_kwh": tesla_history.load_history_config().usable_kwh,
        "sample": None,
        "state": None,
        "checked_at": None,
    }
    if out["configured"]:
        tesla_history.init_db()  # a fresh database has no tables yet
        latest = tesla_history.latest()
        out.update(sample=latest["sample"], state=latest["state"], checked_at=latest["checked_at"])
    return out


@router.get("/api/tesla")
async def get_tesla() -> Dict[str, Any]:
    """Whether the car is set up, its latest stored sample and the last state seen."""
    return await asyncio.to_thread(_status)

