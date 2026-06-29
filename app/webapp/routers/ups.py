"""Local USB UPS status API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.notify_config import is_notify_configured
from src.power_notify_prefs import (
    PowerNotifyPrefs,
    load_power_notify_prefs,
    save_power_notify_prefs,
)
from src.ups_client import fetch_ups_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/ups")
async def get_ups() -> Dict[str, Any]:
    """Return local UPS telemetry from NUT or the Windows USB-HID battery driver."""
    state = await asyncio.to_thread(fetch_ups_state)
    return {"ups": state.to_dict()}


def _notify_prefs_payload(prefs: PowerNotifyPrefs) -> Dict[str, Any]:
    return {"prefs": asdict(prefs), "telegram_configured": is_notify_configured()}


@router.get("/api/ups/notify-prefs")
async def get_power_notify_prefs() -> Dict[str, Any]:
    """Return the UPS power-event notification toggles + whether Telegram is set up."""
    try:
        return _notify_prefs_payload(load_power_notify_prefs())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load power notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load notify prefs: {exc}")


@router.put("/api/ups/notify-prefs")
async def update_power_notify_prefs(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    current = asdict(load_power_notify_prefs())
    updated = {key: bool(body.get(key, current[key])) for key in current}
    try:
        prefs = PowerNotifyPrefs(**updated)
        save_power_notify_prefs(prefs)
        return _notify_prefs_payload(prefs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save power notify prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save notify prefs: {exc}")
