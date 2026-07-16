"""Thin async client for Voice Transcriber's supported session API.

This is the same fleet integration App Launcher uses: create a session, stream
one-second browser chunks to disk, consume rolling SSE partials, then finish for
the canonical transcript.  Voice Transcriber owns audio persistence and the
shared Whisper process; this app never loads a model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlencode

import aiohttp
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://127.0.0.1:8443"


def load_base_url() -> str:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    return os.getenv("VOICE_TRANSCRIBER_URL", DEFAULT_BASE_URL).strip().rstrip("/")


class VoiceTranscriberError(RuntimeError):
    """A distinct Voice Transcriber transport/API failure."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class VoiceTranscriberClient:
    """Async wrapper over the stable ``/api/sessions`` contract."""

    def __init__(self, session: aiohttp.ClientSession, base_url: Optional[str] = None) -> None:
        self.session = session
        self.base_url = (base_url if base_url is not None else load_base_url()).rstrip("/")
        if not self.base_url:
            raise VoiceTranscriberError("VOICE_TRANSCRIBER_URL is not configured", status=503)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 90,
    ) -> Dict[str, Any]:
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                data=body,
                headers=headers,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    raise VoiceTranscriberError(
                        str(detail or f"Voice Transcriber returned HTTP {response.status}"),
                        status=502 if response.status >= 500 else response.status,
                    )
                return payload if isinstance(payload, dict) else {}
        except VoiceTranscriberError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise VoiceTranscriberError(
                f"Voice Transcriber is offline or unreachable: {exc}", status=503
            ) from exc

    async def create_session(self, language: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"source": "home-automation"}
        if language:
            body["language"] = language
        return await self._request("POST", "/api/sessions", json_body=body, timeout=15)

    async def send_chunk(self, session_id: str, content: bytes, content_type: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/chunk",
            body=content,
            headers={"Content-Type": content_type},
        )

    async def finish(self, session_id: str, language: Optional[str] = None) -> Dict[str, Any]:
        suffix = f"?{urlencode({'language': language})}" if language else ""
        return await self._request("POST", f"/api/sessions/{session_id}/finish{suffix}")

    async def upload(
        self,
        filename: str,
        content: bytes,
        content_type: str,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        created = await self.create_session(language)
        session_id = created.get("session_id")
        if not session_id:
            raise VoiceTranscriberError("Voice Transcriber returned no session_id")
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type=content_type)
        suffix = f"?{urlencode({'language': language})}" if language else ""
        try:
            async with self.session.post(
                f"{self.base_url}/api/sessions/{session_id}/upload{suffix}",
                data=form,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    raise VoiceTranscriberError(
                        str(detail or f"Voice Transcriber returned HTTP {response.status}"),
                        status=502 if response.status >= 500 else response.status,
                    )
                return payload if isinstance(payload, dict) else {}
        except VoiceTranscriberError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise VoiceTranscriberError(
                f"Voice Transcriber is offline or unreachable: {exc}", status=503
            ) from exc

    async def events(self, session_id: str) -> AsyncIterator[bytes]:
        """Yield the upstream SSE stream without buffering."""

        try:
            async with self.session.get(
                f"{self.base_url}/api/sessions/{session_id}/events",
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as response:
                if response.status >= 400:
                    raise VoiceTranscriberError(
                        f"Voice Transcriber event stream returned HTTP {response.status}"
                    )
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
        except VoiceTranscriberError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise VoiceTranscriberError(
                f"Voice Transcriber is offline or unreachable: {exc}", status=503
            ) from exc
