"""Voice-created Google Calendar events (issue #313).

Create-only, voice-only: no ``GET``/``PUT`` CRUD like ``reminders.py`` or
``wake_alarms.py`` — Google Calendar itself is the only store, so there's no
local list for the PWA to show or edit. Mirrors the "always speak something"
convention: every path returns 200 with ``{ok, speech}``, never a bare
4xx/5xx, including OAuth/token failures — HA Assist always has something
to say back.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from src.calendar_events import (
    describe_calendar_event,
    parse_spoken_calendar_event,
    to_google_event_body,
)
from src.calendar_write import CalendarWriteError, insert_event, timezone_name

logger = logging.getLogger(__name__)

router = APIRouter()


class VoicePhrasePayload(BaseModel):
    phrase: str = ""


@router.post("/api/calendar/voice")
async def voice_add_calendar_event(payload: VoicePhrasePayload) -> Dict[str, Any]:
    """Create a Google Calendar event from a spoken phrase (HA Assist →
    ``rest_command``). The HA ``intent_script`` speaks ``speech`` back
    verbatim."""

    parsed = parse_spoken_calendar_event(payload.phrase, datetime.now())
    if parsed is None:
        return {
            "ok": False,
            "speech": "Sorry, I need at least a date to add that to your calendar.",
        }

    body = to_google_event_body(parsed, timezone_name())
    try:
        created = insert_event(body)
    except CalendarWriteError as exc:
        logger.warning("⚠️ Failed to create calendar event: %s", exc)
        return {"ok": False, "speech": "Sorry, I couldn't reach your calendar."}

    described = describe_calendar_event(parsed)
    return {
        "ok": True,
        "id": created.get("id"),
        "summary": parsed["summary"],
        "html_link": created.get("htmlLink"),
        "speech": f"Added {described} to your calendar.",
    }
