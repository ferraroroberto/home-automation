"""Automatic-alarm notification toggles CRUD over ``src.alarm_notify_prefs``.

Split out of ``routers/security.py`` (issue #346) — same rationale as
``security_schedules.py``: the seven per-event Telegram toggles are fully
self-contained and share no state with the live RISCO read/write path that
stays in ``security.py``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import _json_body
from src.alarm_notify_prefs import (
    AlarmNotifyPrefs,
    load_alarm_notify_prefs,
    save_alarm_notify_prefs,
)
from src.notify_config import is_notify_configured

logger = logging.getLogger(__name__)

router = APIRouter()


def _notify_prefs_payload(prefs: AlarmNotifyPrefs) -> Dict[str, Any]:
    return {
        "prefs": asdict(prefs),
        "telegram_configured": is_notify_configured(),
    }


@router.get("/api/security/notify-prefs")
async def get_notify_prefs() -> Dict[str, Any]:
    """Return the automatic-alarm notification toggles + whether Telegram is set up."""
    try:
        return _notify_prefs_payload(load_alarm_notify_prefs())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load notify prefs: {exc}")


@router.put("/api/security/notify-prefs")
async def update_notify_prefs(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    current = asdict(load_alarm_notify_prefs())
    updated = {key: bool(body.get(key, current[key])) for key in current}
    try:
        prefs = AlarmNotifyPrefs(**updated)
        save_alarm_notify_prefs(prefs)
        return _notify_prefs_payload(prefs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save notify prefs: {exc}")
