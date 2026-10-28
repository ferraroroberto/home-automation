"""Response compression (#756): gzip on documents and JSON, never on streams.

``/`` must leave gzipped for a client that asks for it. The camera MJPEG
stream and the dictation SSE must pass through uncompressed: the gzip
compressor holds small writes back, which would stall frames and events.
No camera or voice backend is touched — both sources are monkeypatched.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

_GZIP = {"Accept-Encoding": "gzip"}


def test_index_is_gzipped_when_accepted(client: TestClient) -> None:
    resp = client.get("/", headers=_GZIP)
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert "<html" in resp.text.lower()  # httpx decodes it transparently


def test_main_script_is_gzipped_when_accepted(client: TestClient) -> None:
    resp = client.get("/static/main.js", headers=_GZIP)
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert "Accept-Encoding" in resp.headers["vary"]


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


# ------------------------------------------------------- stream immediacy
#
# TestClient buffers a whole response, so it cannot see a stream held back
# inside the compressor. These drive the real app as a raw ASGI call and gate
# the stream's second chunk on the first one having reached the client: a
# middleware that buffers never opens the gate and the stream breaks.


async def _drain(path: str, first_chunk_seen: asyncio.Event) -> bytes:
    from app.webapp.server import app

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"accept-encoding", b"gzip")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    sent: list[bytes] = []

    async def receive() -> dict:
        await asyncio.Event().wait()  # the client never disconnects
        return {"type": "http.disconnect"}

    async def collect(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            sent.append(message["body"])
            first_chunk_seen.set()

    await asyncio.wait_for(app(scope, receive, collect), timeout=5)
    return b"".join(sent)


def test_dictation_sse_delivers_each_event_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.webapp.routers import ha

    first = b"event: partial\ndata: one\n\n"
    second = b"event: partial\ndata: two\n\n"

    async def main() -> bytes:
        seen = asyncio.Event()

        class _FakeVoice:
            async def events(self, session_id: str):
                yield first
                await asyncio.wait_for(seen.wait(), timeout=2)
                yield second

        @asynccontextmanager
        async def _fake_voice_client(request):
            yield _FakeVoice()

        monkeypatch.setattr(ha, "_voice_client", _fake_voice_client)
        return await _drain("/api/ha/transcribe/sessions/abc123/events", seen)

    assert asyncio.run(main()) == first + second


def test_camera_mjpeg_delivers_each_frame_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.webapp.routers import cameras

    frame_one = b"\xff\xd8" + b"\x01" * 50 + b"\xff\xd9"
    frame_two = b"\xff\xd8" + b"\x02" * 50 + b"\xff\xd9"

    async def main() -> bytes:
        seen = asyncio.Event()

        async def _frames(camera_id: str):
            yield frame_one
            await asyncio.wait_for(seen.wait(), timeout=2)
            yield frame_two

        monkeypatch.setattr(cameras, "mjpeg_frames", _frames)
        return await _drain("/api/cameras/cam1/stream", seen)

    body = asyncio.run(main())
    assert frame_one in body
    assert frame_two in body
