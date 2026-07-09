"""Wake-alarm + app-native timer API (issue #304).

``GET``/``PUT /api/wake-alarms`` manage the persisted recurring/one-shot
alarm list; ``POST /api/wake-alarms/{id}/test`` and ``.../dismiss`` drive the
"ringing" state the Home card shows. ``/api/wake-timers`` is the separate,
unpersisted countdown-timer pool (see ``src.wake_timers``).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.webapp.routers._helpers import _json_body
from app.webapp.wake_alarm_automation import (
    dismiss_alarm,
    ringing_alarm_ids,
    test_fire_alarm,
)
from src.wake_alarms import (
    describe_alarm,
    load_wake_alarms,
    next_fire,
    parse_spoken_alarm,
    set_wake_alarms,
    soonest_enabled,
)
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


class VoicePhrasePayload(BaseModel):
    phrase: str = ""


@router.post("/api/wake-alarms/voice")
async def voice_set_wake_alarm(payload: VoicePhrasePayload) -> Dict[str, Any]:
    """Create a wake alarm from a spoken phrase (HA Assist → ``rest_command``).

    The HA ``intent_script`` speaks ``speech`` back to the user. A phrase with
    no recognisable time returns ``ok: false`` and a clarifying line rather than
    a 4xx, so the voice assistant always has something to say.
    """

    parsed = parse_spoken_alarm(payload.phrase, datetime.now())
    if parsed is None:
        return {
            "ok": False,
            "speech": "Sorry, I didn't catch a time for that wake alarm.",
        }
    parsed["id"] = f"wake-{uuid4().hex[:6]}"
    try:
        current = load_wake_alarms()
        entries = set_wake_alarms([asdict(e) for e in current] + [parsed])
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to save voice wake alarm: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save wake alarm: {exc}")
    new_entry = next((e for e in entries if e.id == parsed["id"]), entries[-1])
    return {
        "ok": True,
        "id": new_entry.id,
        "time": new_entry.time,
        "days": new_entry.days,
        "date": new_entry.date,
        "speech": f"Wake alarm set for {describe_alarm(new_entry)}.",
    }


@router.get("/api/wake-alarms/voice")
async def voice_list_wake_alarms() -> Dict[str, Any]:
    """A spoken summary of the enabled wake alarms (HA "what wake alarms…")."""

    entries = [entry for entry in load_wake_alarms() if entry.enabled]
    if not entries:
        return {"count": 0, "speech": "You have no wake alarms set."}
    entries.sort(key=lambda entry: next_fire(entry, datetime.now()))
    parts = [describe_alarm(entry) for entry in entries]
    if len(parts) == 1:
        body = parts[0]
    else:
        body = ", ".join(parts[:-1]) + ", and " + parts[-1]
    plural = "" if len(parts) == 1 else "s"
    return {
        "count": len(parts),
        "speech": f"You have {len(parts)} wake alarm{plural}: {body}.",
    }


@router.post("/api/wake-alarms/voice/cancel")
async def voice_cancel_wake_alarm() -> Dict[str, Any]:
    """Cancel the soonest-upcoming enabled wake alarm (repeat for the next)."""

    try:
        current = load_wake_alarms()
        target = soonest_enabled(current, datetime.now())
        if target is None:
            return {"cancelled": False, "speech": "You have no wake alarms to cancel."}
        set_wake_alarms([asdict(e) for e in current if e.id != target.id])
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to cancel voice wake alarm: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to cancel wake alarm: {exc}")
    return {
        "cancelled": True,
        "id": target.id,
        "speech": f"Cancelled your wake alarm for {describe_alarm(target)}.",
    }


@router.post("/api/wake-alarms/{alarm_id}/test")
async def test_wake_alarm(alarm_id: str) -> Dict[str, Any]:
    if not await test_fire_alarm(alarm_id):
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
