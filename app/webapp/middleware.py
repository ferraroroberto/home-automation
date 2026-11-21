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
  carry the long-lived bearer token.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.secret_compare import secrets_match

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_pc_itself(request: Request) -> bool:
    """True when the caller is this PC: the same check that skips the token.

    The app is reached directly over Tailscale HTTPS (no tunnel or proxy in
    front), so a loopback socket address is the PC itself and never a public caller.
    """
    return (request.client.host if request.client else "") in _LOOPBACK_HOSTS


def require_authenticated(request: Request) -> None:
    """Route dependency: refuse unless :class:`BearerTokenMiddleware` let this call in.

    The middleware marks a request it admitted by auth (no token configured,
    loopback, or a matching token); the exempt paths (``/``, ``/static/*``,
    login) are admitted without that mark. A route that must never be reachable
    on an exempt path, such as the restart, depends on this so it cannot be
    mounted open, and an app built without the middleware refuses it too.
    """
    if not getattr(request.state, "auth_passed", False):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


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
        if not token or is_pc_itself(request):
            request.state.auth_passed = True
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

        if presented and secrets_match(presented, token):
            request.state.auth_passed = True
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="home-automation"'},
        )
