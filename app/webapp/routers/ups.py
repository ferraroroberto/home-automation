"""Local USB UPS status API."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter

from app.webapp.routers._helpers import make_bool_prefs_router
from src.power_notify_prefs import (
    PowerNotifyPrefs,
    load_power_notify_prefs,
    save_power_notify_prefs,
)
from src.ups_client import fetch_ups_state

router = APIRouter()


@router.get("/api/ups")
async def get_ups() -> Dict[str, Any]:
    """Return local UPS telemetry from NUT or the Windows USB-HID battery driver."""
    state = await asyncio.to_thread(fetch_ups_state)
    return {"ups": state.to_dict()}


# The power-event toggles are the same GET/PUT bool-prefs shape as
# ``security_notify.py``'s alarm toggles — one shared factory (issue #664).
router.include_router(
    make_bool_prefs_router(
        load_power_notify_prefs,
        save_power_notify_prefs,
        PowerNotifyPrefs,
        path="/api/ups/notify-prefs",
        log_noun="power notify prefs",
        slug="power_notify_prefs",
        get_doc="Return the UPS power-event notification toggles + whether Telegram is set up.",
    )
)
