"""Page boot, liveness probe, iOS CA profile, and build identity.

Most routes here are unauthenticated entry points (``/``, ``/healthz``,
``/install-ca``). ``/api/version`` is the exception — it is auth-gated like
the rest of the API (loopback bypasses; the PWA attaches the bearer via
``jsonApi``) so the running build's git SHA isn't exposed to unauthenticated
remote callers.
"""

from __future__ import annotations

import datetime as _dt
import logging
import subprocess
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.webapp.routers._helpers import PROJECT_ROOT, STATIC_DIR

_log = logging.getLogger(__name__)

router = APIRouter()

_MOBILECONFIG = "home-automation-ca.mobileconfig"


def _resolve_git_sha() -> str:
    """Short git SHA, captured once at module import.

    Falls back to ``"unknown"`` if git isn't on PATH or this isn't a repo
    (both happen in test envs and shouldn't crash startup). The pythonw tray
    has no console, so ``CREATE_NO_WINDOW`` + ``stdin=DEVNULL`` keep a stray
    cmd from flashing and dodge the invalid-handle trap a console-less parent
    can hit before git even runs.
    """
    cmd = ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"]
    kwargs: Dict[str, Any] = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("⚠️ /api/version: git rev-parse raised %s: %s", type(exc).__name__, exc)
        return "unknown"
    sha = (result.stdout or "").strip()
    if not sha:
        _log.warning(
            "⚠️ /api/version: git rev-parse exit=%s stderr=%r",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return "unknown"
    return sha


_GIT_SHA = _resolve_git_sha()
_BUILT_AT = _dt.datetime.now().replace(microsecond=0).isoformat()


@router.get("/")
async def index() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    # no-cache on the entry document so a webapp restart after an edit is
    # always picked up — no stale iOS PWA cache.
    return HTMLResponse(
        index_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "home-automation-webapp"}


@router.get("/api/version")
async def version() -> Dict[str, str]:
    """Build identity for the PWA footer. Stable across requests; cached at load."""
    return {"git_sha": _GIT_SHA, "built_at": _BUILT_AT}


@router.get("/install-ca")
async def install_ca() -> FileResponse:
    profile = STATIC_DIR / _MOBILECONFIG
    if not profile.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "CA profile not generated yet. Run "
                "`scripts/gen_ssl_cert.py` from the project root."
            ),
        )
    return FileResponse(
        str(profile),
        media_type="application/x-apple-aspen-config",
        filename=_MOBILECONFIG,
    )
