"""Reminders API — bidirectional voice/app sync (issue #314).

``GET``/``PUT /api/reminders`` manage the persisted reminder list for the
PWA; the voice endpoints mirror ``app/webapp/routers/wake_alarms.py``'s
convention — every voice response carries a ``speech`` string, never a bare
4xx, so HA Assist always has something to say back. English only (no
Spanish sentence grammar ships for reminders, unlike wake alarms).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.webapp.routers._helpers import _json_body
from src.reminders import (
    describe_reminder,
    load_reminders,
    next_due,
    parse_spoken_reminder,
    set_reminders,
    soonest_pending,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _reminders_payload(entries: List[Any]) -> Dict[str, Any]:
    pending = [entry for entry in entries if not entry.done]
    return {
        "pending_count": len(pending),
        "entries": [asdict(entry) for entry in entries],
    }


@router.get("/api/reminders")
async def get_reminders() -> Dict[str, Any]:
    try:
        return _reminders_payload(load_reminders())
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to load reminders: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to load reminders: {exc}")


@router.put("/api/reminders")
async def update_reminders(request: Request) -> Dict[str, Any]:
    body = await _json_body(request)
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="'entries' must be a list")
    now_iso = datetime.now().isoformat()
    for item in entries:
        if isinstance(item, dict) and not item.get("created_at"):
            item["created_at"] = now_iso
    try:
        return _reminders_payload(set_reminders(entries))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to save reminders: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save reminders: {exc}")


class VoicePhrasePayload(BaseModel):
    phrase: str = ""


@router.post("/api/reminders/voice")
async def voice_add_reminder(payload: VoicePhrasePayload) -> Dict[str, Any]:
    """Create a reminder from a spoken phrase (HA Assist → ``rest_command``).

    The HA ``intent_script`` speaks ``speech`` back verbatim. A phrase with
    no reminder text (after stripping any due-date/time cue) returns
    ``ok: false`` and a clarifying line rather than a 4xx.
    """

    parsed = parse_spoken_reminder(payload.phrase, datetime.now())
    if parsed is None:
        return {"ok": False, "speech": "Sorry, I didn't catch what to remind you about."}
    parsed["id"] = f"reminder-{uuid4().hex[:6]}"
    parsed["created_at"] = datetime.now().isoformat()
    try:
        current = load_reminders()
        entries = set_reminders([asdict(e) for e in current] + [parsed])
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to save voice reminder: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save reminder: {exc}")
    new_entry = next((e for e in entries if e.id == parsed["id"]), entries[-1])
    described = describe_reminder(new_entry)
    return {
        "ok": True,
        "id": new_entry.id,
        "text": new_entry.text,
        "date": new_entry.date,
        "time": new_entry.time,
        "speech": f"Reminder set: {described}.",
    }


@router.get("/api/reminders/voice")
async def voice_list_reminders() -> Dict[str, Any]:
    """A spoken summary of the pending (not-done) reminders."""

    entries = [entry for entry in load_reminders() if not entry.done]
    if not entries:
        return {"count": 0, "speech": "You have no pending reminders."}
    now = datetime.now()
    entries.sort(key=lambda entry: next_due(entry, now))
    parts = [describe_reminder(entry) for entry in entries]
    body = parts[0] if len(parts) == 1 else "; ".join(parts[:-1]) + ", and " + parts[-1]
    plural = "" if len(parts) == 1 else "s"
    return {"count": len(parts), "speech": f"You have {len(parts)} reminder{plural}: {body}."}


@router.post("/api/reminders/voice/complete")
async def voice_complete_reminder() -> Dict[str, Any]:
    """Mark the soonest-due (or oldest undated) pending reminder done."""

    try:
        current = load_reminders()
        target = soonest_pending(current, datetime.now())
        if target is None:
            return {"done": False, "speech": "You have no pending reminders to complete."}
        updated = [
            {**asdict(e), "done": True} if e.id == target.id else asdict(e)
            for e in current
        ]
        set_reminders(updated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to complete voice reminder: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to complete reminder: {exc}")
    described = describe_reminder(target)
    return {"done": True, "id": target.id, "speech": f"Marked done: {described}."}
