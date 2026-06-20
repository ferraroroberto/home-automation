"""Page boot, liveness probe, iOS CA profile, and build identity.

Most routes here are unauthenticated entry points (``/``, ``/healthz``,
``/install-ca``). ``/api/version`` is the exception — it is auth-gated like
the rest of the API (loopback bypasses; the PWA attaches the bearer via
``jsonApi``) so the running build's git SHA isn't exposed to unauthenticated
remote callers.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.webapp.routers._helpers import BUILD_INFO, STATIC_DIR

router = APIRouter()

_MOBILECONFIG = "home-automation-ca.mobileconfig"


@router.get("/")
async def index() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html missing")
    # Stamp the asset URLs with the build fleet hash and force the entry
    # document to revalidate, so a tray restart after an edit is always
    # picked up — no stale iOS PWA cache.
    html = BUILD_INFO.stamp_html(index_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "home-automation-webapp"}


@router.get("/api/version")
async def version() -> Dict[str, str]:
    """Build identity for the PWA footer. Stable across requests; cached at load."""
    return BUILD_INFO.as_dict()


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
