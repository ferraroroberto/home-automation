"""Page boot, liveness probe, and build identity.

Most routes here are unauthenticated entry points (``/``, ``/healthz``).
``/api/version`` is the exception — it is auth-gated like the rest of the API
(loopback bypasses; the PWA attaches the bearer via ``jsonApi``) so the running
build's git SHA isn't exposed to unauthenticated remote callers.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.webapp.routers._helpers import BUILD_INFO, STATIC_DIR

router = APIRouter()


# A line holding only this comment is replaced by the named file's contents:
# each view's markup lives in static/views/, the shell stays in index.html.
_INCLUDE_RE = re.compile(r"^[ \t]*<!-- @include ([\w\-./]+\.html) -->[ \t]*\n", re.M)

_FileKey = Tuple[str, int, int]


def _file_key(path: Path) -> _FileKey:
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def _expanded_index(index_path: Path) -> Tuple[str, List[_FileKey]]:
    """index.html with each ``@include`` line replaced by its view partial.

    Returns the document and the key of every file read, so the caller can
    tell when any of them changed on disk. A partial that is missing or
    escapes ``static/`` fails the request rather than serving a page with a
    hole in it.
    """
    static_root = index_path.parent.resolve()
    keys: List[_FileKey] = [_file_key(index_path)]

    def _include(match: "re.Match[str]") -> str:
        partial = (static_root / match.group(1)).resolve()
        if static_root not in partial.parents:
            raise HTTPException(status_code=500, detail="index include outside static/")
        try:
            keys.append(_file_key(partial))
            return partial.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(
                status_code=500, detail=f"index include missing: {match.group(1)}"
            )

    body = _INCLUDE_RE.sub(_include, index_path.read_text(encoding="utf-8"))
    return body, keys


def _keys_unchanged(keys: List[_FileKey]) -> bool:
    try:
        return all(_file_key(Path(path)) == (path, mtime, size) for path, mtime, size in keys)
    except OSError:
        return False


def _stamped_index(app: Any) -> Tuple[str, str]:
    """The stamped index.html and its ETag, re-rendered only on change (#757).

    The body is cached on the app, keyed on the path, mtime and size of
    index.html and of every view partial it includes, so an edit on disk is
    picked up on the next load without a restart; the asset hashes it is
    stamped with are fixed per process. The ETag is taken over the stamped
    body, which embeds the build fleet hash.
    """
    index_path = STATIC_DIR / "index.html"
    cached = getattr(app.state, "index_cache", None)
    if cached is not None:
        keys = cached[0]
        # keys[0] is index.html itself, so a different STATIC_DIR misses too.
        if keys[0][0] == str(index_path) and _keys_unchanged(keys):
            return cached[1], cached[2]
    try:
        expanded, keys = _expanded_index(index_path)
    except OSError:
        raise HTTPException(status_code=500, detail="index.html missing")
    stamped = BUILD_INFO.stamp_html(expanded)
    etag = '"' + hashlib.sha256(stamped.encode("utf-8")).hexdigest()[:20] + '"'
    app.state.index_cache = (keys, stamped, etag)
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
