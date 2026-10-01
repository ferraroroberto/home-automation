"""Response compression (#756): gzip on documents and JSON, never on streams.

``/`` must leave gzipped for a client that asks for it. The camera MJPEG
stream and the dictation SSE must pass through uncompressed: the gzip
compressor holds small writes back, which would stall frames and events.
No camera or voice backend is touched — both sources are monkeypatched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

_GZIP = {"Accept-Encoding": "gzip"}


def test_index_is_gzipped_when_accepted(client: TestClient) -> None:
    resp = client.get("/", headers=_GZIP)
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert "<html" in resp.text.lower()  # httpx decodes it transparently


def test_index_is_plain_without_accept_encoding(client: TestClient) -> None:
    resp = client.get("/", headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_dictation_sse_is_not_compressed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.webapp.routers import ha

    class _FakeVoice:
        async def events(self, session_id: str):
            yield b"event: partial\ndata: " + b"x" * 2000 + b"\n\n"

    @asynccontextmanager
    async def _fake_voice_client(request):
        yield _FakeVoice()

    monkeypatch.setattr(ha, "_voice_client", _fake_voice_client)

    resp = client.get("/api/ha/transcribe/sessions/abc123/events", headers=_GZIP)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in resp.headers


def test_camera_mjpeg_stream_is_not_compressed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.webapp.routers import cameras

    async def _frames(camera_id: str):
        yield b"\xff\xd8" + b"\x00" * 4000 + b"\xff\xd9"

    monkeypatch.setattr(cameras, "mjpeg_frames", _frames)

    resp = client.get("/api/cameras/cam1/stream", headers=_GZIP)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert "content-encoding" not in resp.headers
