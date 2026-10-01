"""Local USB UPS status API."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter

from app.webapp.read_snapshot import ReadSnapshot
from app.webapp.routers._helpers import make_bool_prefs_router
from src.power_notify_prefs import (
    PowerNotifyPrefs,
    load_power_notify_prefs,
    save_power_notify_prefs,
)
from src.ups_client import UpsState, fetch_ups_state

router = APIRouter()

# ``upsc`` shells out with a 5 s timeout and hit it about once a minute, which
# landed inside a request (#769). It runs off the request path instead, at the
# PWA's 15 s poll; a slow or failed read shows as the snapshot's age or the
# state's own ``error``, never as request latency. ``max_age_s`` clears one
# tick plus one full 5 s timeout, so a slow tick never makes a reader fetch.
UPS_SNAPSHOT: ReadSnapshot[UpsState] = ReadSnapshot(
    "ups",
    lambda: asyncio.to_thread(fetch_ups_state),  # looked up per call, so tests can patch it
    max_age_s=30.0,
    tick_s=10.0,
)


@router.get("/api/ups")
async def get_ups() -> Dict[str, Any]:
    """Return local UPS telemetry from NUT or the Windows USB-HID battery driver."""
    state, snapshot = await UPS_SNAPSHOT.read()
    return {"ups": state.to_dict(), "snapshot": snapshot}


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
