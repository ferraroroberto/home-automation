"""Over-budget request breadcrumbs for the webapp (#761).

The tray launches uvicorn with its output discarded and ``webapp.log`` carries
app breadcrumbs, not request timings, so a speed regression on the paths the
phone opens on would go unnoticed until someone feels it. This middleware
leaves a line in ``webapp/slow-requests.log``:

* one per GET over its :data:`HOT_PATH_BUDGET_S` budget — ``/`` and the API
  reads the phone polls (the set ``/perf-review`` scores);
* one per request of any method slower than :data:`SLOW_REQUEST_S`.

Ported from app-launcher's ``app/webapp/observability.py`` (fleet-config#1121
playbook P10), minus its in-flight counter. Pure ASGI, not
``BaseHTTPMiddleware``: every message passes straight through, so nothing is
buffered. Streams (dictation SSE, camera MJPEG) are never logged — how long
one stays open is its lifetime, not its latency.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.webapp.routers._helpers import PROJECT_ROOT

logger = logging.getLogger(__name__)
slow_logger = logging.getLogger("home_automation.slowreq")

_SLOW_LOG_PATH = PROJECT_ROOT / "webapp" / "slow-requests.log"

SLOW_REQUEST_S = 3.0
_API_BUDGET_S = 0.25
HOT_PATH_BUDGET_S = {
    path: _API_BUDGET_S
    for path in (
        "/", "/api/units", "/api/energy", "/api/security", "/api/hyperv",
        "/api/tuya", "/api/ups", "/api/searxng", "/api/presence",
        "/api/reminders", "/api/wake-alarms", "/api/wake-timers",
        "/api/weather", "/api/version",
    )
}

_STREAM_CONTENT_TYPES = (b"text/event-stream", b"multipart/x-mixed-replace")


def ensure_slow_log_handler() -> None:
    """Attach the rotating ``slow-requests.log`` handler once; idempotent."""
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _SLOW_LOG_PATH.resolve()
        for h in slow_logger.handlers
    ):
        return
    try:
        _SLOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            _SLOW_LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fh.setLevel(logging.WARNING)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        slow_logger.addHandler(fh)
        slow_logger.setLevel(logging.WARNING)
    except OSError as exc:
        logger.warning("⚠️ Could not open %s: %s", _SLOW_LOG_PATH, exc)


class SlowRequestLogMiddleware:
    """ASGI middleware: log over-budget GETs and slow requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        start = time.monotonic()
        seen = {"status": 0, "stream": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                seen["status"] = message.get("status", 0)
                content_type = dict(message.get("headers") or []).get(b"content-type", b"")
                seen["stream"] = content_type.startswith(_STREAM_CONTENT_TYPES)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not seen["stream"]:
                _log_if_slow(method, path, seen["status"], time.monotonic() - start)


def _log_if_slow(method: str, path: str, status: int, elapsed: float) -> None:
    if elapsed >= SLOW_REQUEST_S:
        slow_logger.warning(
            "🐢 slow request: %s %s → %s in %.1fs", method, path, status or "?", elapsed
        )
    elif method == "GET" and elapsed >= HOT_PATH_BUDGET_S.get(path, float("inf")):
        slow_logger.warning(
            "⏱️ over budget: GET %s → %s in %.2fs (budget %.2fs)",
            path, status or "?", elapsed, HOT_PATH_BUDGET_S[path],
        )
