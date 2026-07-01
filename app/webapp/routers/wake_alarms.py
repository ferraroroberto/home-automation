"""Wake-alarm + app-native timer API (issue #304).

``GET``/``PUT /api/wake-alarms`` manage the persisted recurring/one-shot
alarm list; ``POST /api/wake-alarms/{id}/test`` and ``.../dismiss`` drive the
"ringing" state the Home card shows. ``/api/wake-timers`` is the separate,
unpersisted countdown-timer pool (see ``src.wake_timers``).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.webapp.wake_alarm_automation import (
    dismiss_alarm,
    ringing_alarm_ids,
    test_fire_alarm,
)
from src.wake_alarms import load_wake_alarms, set_wake_alarms
from src.wake_timers import cancel_timer, create_timer, list_timers

logger = logging.getLogger(__name__)

router = APIRouter()


def _alarms_payload(entries: List[Any]) -> Dict[str, Any]:
    ringing = ringing_alarm_ids()
    active = [entry for entry in entries if entry.enabled]
    return {
        "enabled": bool(active),
        "count": len(active),
        "entries": [
            {**asdict(entry), "ringing": entry.id in ringing} for entry in entries
        ],
    }


def _timer_dict(entry: Any) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "label": entry.label,
        "seconds": entry.seconds,
        "ends_at": entry.ends_at,
        "ringing": entry.ringing,
    }


@router.get("/api/wake-alarms")
async def get_wake_alarms() -> Dict[str, Any]:
    try:
        return _alarms_payload(load_wake_alarms())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to load wake alarms: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load wake alarms: {exc}")


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


@router.put("/api/wake-alarms")
async def update_wake_alarms(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    try:
        return _alarms_payload(set_wake_alarms(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to save wake alarms: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save wake alarms: {exc}")


@router.post("/api/wake-alarms/{alarm_id}/test")
async def test_wake_alarm(alarm_id: str) -> Dict[str, Any]:
    if not test_fire_alarm(alarm_id):
        raise HTTPException(status_code=404, detail=f"unknown alarm '{alarm_id}'")
    return {"id": alarm_id, "ringing": True}


@router.post("/api/wake-alarms/{alarm_id}/dismiss")
async def dismiss_wake_alarm(alarm_id: str) -> Dict[str, Any]:
    dismissed = dismiss_alarm(alarm_id)
    return {"id": alarm_id, "ringing": False, "dismissed": dismissed}


class CreateTimerPayload(BaseModel):
    label: str = ""
    seconds: int = Field(gt=0)


@router.get("/api/wake-timers")
async def get_wake_timers() -> Dict[str, Any]:
    return {"timers": [_timer_dict(t) for t in list_timers()]}


@router.post("/api/wake-timers")
async def post_wake_timer(payload: CreateTimerPayload) -> Dict[str, Any]:
    entry = create_timer(payload.label, payload.seconds)
    return _timer_dict(entry)


@router.delete("/api/wake-timers/{timer_id}")
async def delete_wake_timer(timer_id: str) -> Dict[str, Any]:
    cancelled = cancel_timer(timer_id)
    return {"id": timer_id, "cancelled": cancelled}
