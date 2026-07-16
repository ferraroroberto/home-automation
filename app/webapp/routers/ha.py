"""Home Assistant Voice PE state, push-to-talk, and interaction-log routes."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import telemetry
from src.ha_client import HaClientError, HomeAssistantClient
from src.voice_client import VoiceTranscriberClient, VoiceTranscriberError, load_base_url

logger = logging.getLogger(__name__)
router = APIRouter()

_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_SATELLITE_ID = re.compile(r"^assist_satellite\.[a-z0-9_]+$")
_MAX_CHUNK_BYTES = 2 * 1024 * 1024
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AnnouncePayload(BaseModel):
    text: str


def _safe_session_id(value: str) -> str:
    if not _SESSION_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid transcription session id")
    return value


def _interaction_rows(events: list[Dict[str, Any]], satellites: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    rooms = {row["entity_id"]: row.get("room") for row in satellites}
    result = []
    for event in events:
        payload = dict(event.get("payload") or {})
        if not payload.get("timestamp"):
            payload["timestamp"] = event.get("ts")
        payload["room"] = rooms.get(payload.get("satellite_id")) or payload.get("room")
        result.append(payload)
    return result


@router.get("/api/ha")
async def get_home_assistant(request: Request) -> Dict[str, Any]:
    """Return HA-owned Voice PE room/state data and compact recent interactions."""

    try:
        async with _http_session(request) as session:
            satellites = await HomeAssistantClient(session).satellites()
    except HaClientError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    try:
        events = await asyncio.to_thread(
            telemetry.read_events, domain="ha_voice", limit=12
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read HA voice interactions: %s", exc)
        events = []
    return {
        "satellites": satellites,
        "interactions": _interaction_rows(events, satellites),
        "voice_transcriber": bool(load_base_url()),
    }


@router.post("/api/ha/satellites/{entity_id:path}/announce")
async def announce(
    entity_id: str, payload: AnnouncePayload, request: Request
) -> Dict[str, Any]:
    """Announce finalized push-to-talk text on one online Assist satellite."""

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="nothing heard — transcript is empty")
    if not _SATELLITE_ID.fullmatch(entity_id):
        raise HTTPException(status_code=400, detail="invalid Assist satellite entity")
    try:
        async with _http_session(request) as session:
            client = HomeAssistantClient(session)
            satellite_state = await client.state(entity_id)
            if str(satellite_state.get("state") or "unknown") in {"unknown", "unavailable"}:
                raise HTTPException(status_code=409, detail="Assist satellite is offline")
            await client.announce(entity_id, text)
    except HTTPException:
        raise
    except HaClientError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))

    interaction = {
        "run_id": f"ptt:{entity_id}:{time.time_ns()}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pipeline": "push-to-talk",
        "satellite_id": entity_id,
        "transcript": text,
        "intent_kind": "direct",
        "intent": "assist_satellite.announce",
        "action": "assist_satellite.announce",
        "spoken_response": text,
        "outcome": "ok",
        "detail": text,
    }
    try:
        await asyncio.to_thread(
            telemetry.record_event,
            "ha_voice",
            "push_to_talk",
            entity_id=entity_id,
            source="home-automation",
            outcome="ok",
            payload=interaction,
        )
    except Exception as exc:  # noqa: BLE001 — announcement already succeeded
        logger.warning("⚠️  Could not record push-to-talk interaction: %s", exc)
    return {"ok": True, "satellite": entity_id, "text": text}


@asynccontextmanager
async def _http_session(request: Optional[Request]):
    """Reuse the lifespan-owned pool; create a temporary one in no-lifespan tests."""

    shared = getattr(request.app.state, "outbound_http", None) if request else None
    if shared is not None and not shared.closed:
        yield shared
        return
    async with aiohttp.ClientSession() as session:
        yield session


@asynccontextmanager
async def _voice_client(request: Request):
    async with _http_session(request) as session:
        yield VoiceTranscriberClient(session)


@router.post("/api/ha/transcribe/sessions")
async def transcribe_create(request: Request) -> Dict[str, Any]:
    language = (request.query_params.get("language") or "").strip() or None
    try:
        async with _voice_client(request) as client:
            return await client.create_session(language)
    except VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/api/ha/transcribe/sessions/{session_id}/chunk")
async def transcribe_chunk(session_id: str, request: Request) -> Dict[str, Any]:
    session_id = _safe_session_id(session_id)
    content = await request.body()
    if len(content) > _MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="audio chunk exceeds 2 MB limit")
    if not content:
        return {"session_id": session_id, "raw_bytes": 0}
    content_type = request.headers.get("content-type") or "audio/webm"
    try:
        async with _voice_client(request) as client:
            return await client.send_chunk(session_id, content, content_type)
    except VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/api/ha/transcribe/sessions/{session_id}/finish")
async def transcribe_finish(session_id: str, request: Request) -> Dict[str, Any]:
    session_id = _safe_session_id(session_id)
    language = (request.query_params.get("language") or "").strip() or None
    try:
        async with _voice_client(request) as client:
            return await client.finish(session_id, language)
    except VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/api/ha/transcribe/sessions/{session_id}/events")
async def transcribe_events(session_id: str, request: Request) -> StreamingResponse:
    session_id = _safe_session_id(session_id)

    async def pump():
        try:
            async with _voice_client(request) as client:
                async for chunk in client.events(session_id):
                    yield chunk
        except VoiceTranscriberError as exc:
            logger.info("Voice Transcriber SSE ended: %s", exc)
            yield f"event: error\ndata: {str(exc)}\n\n".encode()

    return StreamingResponse(
        pump(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/ha/transcribe")
async def transcribe_upload(
    request: Request, file: UploadFile = File(...)
) -> Dict[str, Any]:
    language: Optional[str] = (request.query_params.get("language") or "").strip() or None
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="empty recording")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="recording exceeds 25 MB limit")
    try:
        async with _voice_client(request) as client:
            return await client.upload(
                file.filename or "recording.webm",
                content,
                file.content_type or "audio/webm",
                language,
            )
    except VoiceTranscriberError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
