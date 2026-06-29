"""Bearer-token / loopback auth middleware for the webapp.

Mirrors the sibling fleet apps (photo-ocr, app-launcher):

- Empty configured token → short-circuit, no gate.
- Loopback callers bypass (local probes keep working tokenless).
- ``/``, ``/static/*``, ``/healthz``, ``/api/login`` stay reachable so the
  page can boot and swap a password for the token.
- Otherwise accept the token from ``Authorization: Bearer …`` or
  ``?token=…``.
- For camera stream/snapshot paths only: also accept ``?camera_token=…``
  (a short-lived HMAC-signed scoped token) so ``<img src>`` URLs never
  carry the long-lived bearer token (issue #261).
"""

from __future__ import annotations

import hmac
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_AUTH_EXEMPT_PREFIXES = (
    "/static/",
    "/healthz",
    "/api/presence/webhook",
)
_AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/api/login"})

# Camera paths where <img src> must supply a scoped token instead of the bearer.
_CAMERA_STREAM_SUFFIXES = ("/stream", "/last_snapshot")


def _is_camera_stream_path(path: str) -> bool:
    if not path.startswith("/api/cameras/"):
        return False
    return any(path.endswith(s) for s in _CAMERA_STREAM_SUFFIXES)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on API endpoints."""

    def __init__(
        self,
        app,
        get_token: Callable[[], str],
        verify_camera_token: Optional[Callable[[str], bool]] = None,
    ):
        super().__init__(app)
        self._get_token = get_token
        self._verify_camera_token = verify_camera_token

    async def dispatch(self, request: Request, call_next):
        token = (self._get_token() or "").strip()
        if not token:
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        if client_host in _LOOPBACK_HOSTS:
            return await call_next(request)

        path = request.url.path
        if path in _AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        # Short-lived scoped token for camera stream/snapshot img-src URLs.
        if self._verify_camera_token is not None and _is_camera_stream_path(path):
            cam_tok = request.query_params.get("camera_token", "").strip()
            if cam_tok and self._verify_camera_token(cam_tok):
                return await call_next(request)

        presented = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented = auth_header[7:].strip()
        if not presented:
            presented = request.query_params.get("token", "").strip()

        if presented and hmac.compare_digest(presented, token):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="home-automation"'},
        )
