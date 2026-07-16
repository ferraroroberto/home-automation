"""Voice-command cheat sheet API (issue #437).

``GET /api/voice-commands`` serves the curated catalogue in ``src.voice_commands``
for the Home tab's "What can I say?" card. Static, read-only reference — no
device I/O, no secrets, nothing to configure.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from src.voice_commands import load_voice_commands

router = APIRouter()


@router.get("/api/voice-commands")
async def get_voice_commands() -> Dict[str, Any]:
    groups = load_voice_commands()
    return {"groups": groups, "count": sum(len(g["commands"]) for g in groups)}
