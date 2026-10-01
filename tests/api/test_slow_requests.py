"""Over-budget request breadcrumbs (#761).

A GET over its budget writes exactly one line to ``slow-requests.log`` and a
fast one writes none. Streams pass through unbuffered and are never logged,
since how long one stays open is not latency. The log file is redirected to
``tmp_path``; the timed routes live on a throwaway app, not the real routers.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterator, List

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.webapp import observability
from app.webapp.observability import SlowRequestLogMiddleware

_DELAY_S = 0.1


@pytest.fixture
def slow_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "slow-requests.log"
    monkeypatch.setattr(observability, "_SLOW_LOG_PATH", path)
    monkeypatch.setattr(observability, "HOT_PATH_BUDGET_S", {"/slow": 0.05, "/fast": 0.05, "/sse": 0.05})
    # Detach the real handler for the test, so no line reaches webapp/.
    monkeypatch.setattr(observability.slow_logger, "handlers", [])
    observability.ensure_slow_log_handler()
    yield path
    for handler in observability.slow_logger.handlers:
        handler.close()


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(_DELAY_S)
        return {"ok": True}

    @app.get("/sse")
    async def sse():
        async def events():
            await asyncio.sleep(_DELAY_S)
            yield b"data: x\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    app.add_middleware(SlowRequestLogMiddleware)
    return TestClient(app)


def _lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_over_budget_get_writes_exactly_one_line(slow_log: Path) -> None:
    assert _client().get("/slow").status_code == 200
    lines = _lines(slow_log)
    assert len(lines) == 1
    assert "over budget: GET /slow → 200" in lines[0]
    assert "(budget 0.05s)" in lines[0]


def test_fast_get_writes_nothing(slow_log: Path) -> None:
    assert _client().get("/fast").status_code == 200
    assert _lines(slow_log) == []


def test_request_over_the_slow_floor_logs_any_method(
    slow_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observability, "SLOW_REQUEST_S", 0.05)
    assert _client().get("/slow").status_code == 200
    assert len(_lines(slow_log)) == 1
    assert "slow request: GET /slow → 200" in _lines(slow_log)[0]


def test_sse_stream_is_not_logged(slow_log: Path) -> None:
    resp = _client().get("/sse")
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert _lines(slow_log) == []


def test_stream_chunks_pass_through_unbuffered() -> None:
    # Each body chunk must reach the server before the app sends the next one:
    # a buffering middleware would hold them until the response ended.
    forwarded: List[dict] = []
    seen_by_app: List[int] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"multipart/x-mixed-replace; boundary=frame")]})
        for i in range(3):
            await send({"type": "http.response.body", "body": b"frame%d" % i, "more_body": i < 2})
            seen_by_app.append(len(forwarded))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        forwarded.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/cameras/1/stream", "headers": []}
    asyncio.run(SlowRequestLogMiddleware(app)(scope, receive, send))
    assert seen_by_app == [2, 3, 4]
    assert [m.get("body") for m in forwarded[1:]] == [b"frame0", b"frame1", b"frame2"]


def test_app_wires_the_middleware_outermost(client: TestClient) -> None:
    assert client.app.user_middleware[0].cls is SlowRequestLogMiddleware
    handlers = logging.getLogger("home_automation.slowreq").handlers
    assert any(
        isinstance(h, logging.FileHandler) and Path(h.baseFilename).name == "slow-requests.log"
        for h in handlers
    )
