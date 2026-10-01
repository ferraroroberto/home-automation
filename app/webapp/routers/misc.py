"""Page boot, liveness probe, and build identity.

Most routes here are unauthenticated entry points (``/``, ``/healthz``).
``/api/version`` is the exception — it is auth-gated like the rest of the API
(loopback bypasses; the PWA attaches the bearer via ``jsonApi``) so the running
build's git SHA isn't exposed to unauthenticated remote callers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.webapp.routers._helpers import BUILD_INFO, STATIC_DIR

router = APIRouter()


def _stamped_index(app: Any) -> Tuple[str, str]:
    """The stamped index.html and its ETag, re-rendered only on change (#757).

    The body is cached on the app, keyed on the file's path, mtime and size,
    so an edit on disk is picked up on the next load without a restart; the
    asset hashes it is stamped with are fixed per process. The ETag is taken
    over the stamped body, which embeds the build fleet hash.
    """
    index_path = STATIC_DIR / "index.html"
    try:
        st = index_path.stat()
    except OSError:
        raise HTTPException(status_code=500, detail="index.html missing")
    key = (str(index_path), st.st_mtime_ns, st.st_size)
    cached = getattr(app.state, "index_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1], cached[2]
    stamped = BUILD_INFO.stamp_html(index_path.read_text(encoding="utf-8"))
    etag = '"' + hashlib.sha256(stamped.encode("utf-8")).hexdigest()[:20] + '"'
    app.state.index_cache = (key, stamped, etag)
    return stamped, etag


def _etag_matches(header: str, etag: str) -> bool:
    return any(
        tag.strip().removeprefix("W/") in (etag, "*") for tag in header.split(",")
    )


@router.get("/")
async def index(request: Request) -> Response:
    stamped, etag = _stamped_index(request.app)
    # Force the entry document to revalidate, so a tray restart after an
    # edit is always picked up — no stale iOS PWA cache. The ETag makes that
    # revalidation a body-less 304 while the page hasn't changed.
    headers = {"Cache-Control": "no-cache, must-revalidate", "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return HTMLResponse(stamped, headers=headers)


@router.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "home-automation-webapp"}


@router.get("/api/version")
async def version() -> Dict[str, str]:
    """Build identity for the PWA footer. Stable across requests; cached at load."""
    return BUILD_INFO.as_dict()
