"""Blind schedule CRUD (#871) and the alarm-pairing switch (#875).

``/api/blinds/schedules`` has the same GET/PUT list shape as the alarm
schedules (the shared ``make_list_crud_router`` factory, issue #571): the
browser edits one list and the background engine in
``app.webapp.blind_schedules`` fires it. ``/api/blinds/alarm-pairing`` is the
one on/off switch for the blinds following the automatic alarm.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import HTTPException
from pydantic import BaseModel

from app.webapp.routers._helpers import make_list_crud_router
from src.blind_automation import (
    BlindAlarmPrefs,
    load_blind_alarm_prefs,
    load_blind_schedules,
    save_blind_alarm_prefs,
    set_blind_schedules,
)

logger = logging.getLogger(__name__)

router = make_list_crud_router(
    load_blind_schedules,
    set_blind_schedules,
    path="/api/blinds/schedules",
    noun="schedules",
    log_noun="blind schedules",
    slug="blind_schedules",
    doc="Return the daily blind up/down schedule entries.",
)


class AlarmPairingPayload(BaseModel):
    follow_alarm: bool


@router.get("/api/blinds/alarm-pairing")
async def get_alarm_pairing() -> Dict[str, Any]:
    """Whether the blinds follow the automatic alarm (default off)."""
    try:
        return asdict(load_blind_alarm_prefs())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to load blind alarm pairing: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load alarm pairing: {exc}")


@router.put("/api/blinds/alarm-pairing")
async def update_alarm_pairing(payload: AlarmPairingPayload) -> Dict[str, Any]:
    try:
        prefs = BlindAlarmPrefs(follow_alarm=payload.follow_alarm)
        save_blind_alarm_prefs(prefs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save blind alarm pairing: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save alarm pairing: {exc}")
    return asdict(prefs)
