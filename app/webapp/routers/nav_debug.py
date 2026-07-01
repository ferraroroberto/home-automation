"""Client-side nav-pin diagnostic sink (#300).

The nav-debug toggle in the PWA (``static/nav-debug.js``) posts each event
here instead of rendering it on-screen — an always-visible overlay grew tall
enough during testing to cover the whole viewport and block interaction.
Appending newline-delimited JSON to a gitignored local file means a
reproduction session can be read back directly from disk instead of relying
on a screenshot of a panel that might already have scrolled the interesting
part out of view.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body

from app.webapp.routers._helpers import PROJECT_ROOT

logger = logging.getLogger(__name__)

router = APIRouter()

LOG_PATH = PROJECT_ROOT / "webapp" / "nav_debug.log"


@router.post("/api/nav-debug")
async def record_nav_debug_event(payload: Dict[str, Any] = Body(...)) -> Dict[str, bool]:
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001 — a debug sink must never break the app
        logger.warning("⚠️  nav-debug log write failed: %s", exc)
    return {"ok": True}
