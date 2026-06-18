"""Unauthenticated entry points: page boot, liveness probe, iOS CA profile."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.webapp.routers._helpers import STATIC_DIR

router = APIRouter()

_MOBILECONFIG = "home-automation-ca.mobileconfig"


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
