"""Spanish voice web search (issue #865, part of #842).

``POST /api/voice/search`` takes ``{"query": "..."}`` from HA Assist (the Spanish
pipeline's ``rest_command``) and returns ``{ok, speech, source}``. Follows the
"always speak something" convention of ``calendar_events.py``: every path
returns 200, including SearXNG or hub failures, so HA always has a sentence to
say back. ``source`` names which stage answered (see ``src.web_search``).
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from src.web_search import answer_query

router = APIRouter()


class VoiceSearchPayload(BaseModel):
    query: str = ""


@router.post("/api/voice/search")
async def voice_search(payload: VoiceSearchPayload) -> Dict[str, Any]:
    """Search the web in Spanish and return the spoken answer."""
    return (await answer_query(payload.query)).to_dict()
