"""Bearer-token / loopback auth middleware for the webapp.

Mirrors the sibling fleet apps (photo-ocr, app-launcher):

- Empty configured token → short-circuit, no gate.
- Loopback callers bypass (local probes keep working tokenless).
- ``/``, ``/static/*``, ``/healthz``, ``/install-ca``, ``/api/login``
  stay reachable so the page can boot and swap a password for the token.
- Otherwise accept the token from ``Authorization: Bearer …`` or
  ``?token=…``.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_AUTH_EXEMPT_PREFIXES = (
    "/static/",
    "/healthz",
    "/install-ca",
    "/api/presence/webhook",
)
_AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/install-ca", "/api/login"})


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on API endpoints."""

    def __init__(self, app, get_token):
        super().__init__(app)
        self._get_token = get_token

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
