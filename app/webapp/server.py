r"""FastAPI webapp — mobile-first MELCloud Home control dashboard.

Routes (split across ``app/webapp/routers/``):

    GET  /                       → static/index.html        (misc)
    GET  /static/{file}          → CSS / JS / icons          (static mount)
    GET  /healthz                → liveness probe            (misc)
    GET  /api/version            → build identity (git_sha)  (misc)
    GET  /install-ca             → iOS .mobileconfig         (misc)
    POST /api/login              → password → bearer token   (auth)
    GET  /api/units              → live state of every unit  (units)
    POST /api/units/{id}         → write controls + read back (units)
    GET  /api/energy             → live SMA energy flow       (energy)
    GET  /api/weather            → current weather (Open-Meteo) (weather)
    GET  /api/tuya               → local Tuya devices + watts (tuya)
    POST /api/tuya/{id}/switch   → on/off a Tuya plug/light   (tuya)
    POST /api/tuya/{id}/cover    → open/close/stop a blind     (tuya)
    GET  /api/security           → RISCO alarm state           (security)
    POST /api/security/{action}  → arm/disarm/perimeter alarm  (security)

Run with::

    & .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447
    .\webapp.bat                                              # HTTPS when cert present
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import auth, energy, misc, security, tuya, units, weather
from app.webapp.routers._helpers import BUILD_INFO, STATIC_DIR
from app.webapp.automation import start_automation
from app.webapp.sampler import start_sampler
from src.webapp_config import load_webapp_config

logger = logging.getLogger(__name__)

# Hash-stamped assets get a one-year immutable cache: the fleet hash in
# the query string makes the URL change on every edit, so a stale copy
# can never be served. Icons + manifest revalidate daily — they almost
# never change but we don't want a year of staleness either.
_LONG_CACHE = "public, max-age=31536000, immutable"
_DAY_CACHE = "public, max-age=86400"
_IMMUTABLE_SUFFIXES = frozenset({".js", ".css"})
_DAILY_SUFFIXES = frozenset({".webmanifest", ".png", ".ico"})


class CachingStaticFiles(StaticFiles):
    """``StaticFiles`` with per-file ``Cache-Control`` + JS-import stamping.

    Starlette's mount serves every file with only ``ETag`` /
    ``Last-Modified``, leaving iOS Safari free to heuristic-cache the
    module graph and the PWA shell. This subclass stamps an explicit
    policy keyed on the suffix, and rewrites each served ``.js`` module's
    relative ``import`` URLs to carry the build fleet hash — so an edit to
    any module busts the whole graph.
    """

    def file_response(
        self,
        full_path: "os.PathLike[str]",
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        suffix = path.suffix.lower()

        if suffix == ".js":
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                return super().file_response(
                    full_path, stat_result, scope, status_code
                )
            media_type, _ = mimetypes.guess_type(str(path))
            return Response(
                content=BUILD_INFO.stamp_js(body),
                status_code=status_code,
                media_type=media_type or "text/javascript",
                headers={"Cache-Control": _LONG_CACHE},
            )

        response = super().file_response(
            full_path, stat_result, scope, status_code
        )
        if suffix in _IMMUTABLE_SUFFIXES:
            response.headers["Cache-Control"] = _LONG_CACHE
        elif suffix in _DAILY_SUFFIXES:
            response.headers["Cache-Control"] = _DAY_CACHE
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the background energy sampler + HVAC automation for the process life."""
    tasks = [t for t in (start_sampler(), start_automation()) if t is not None]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    """Build the FastAPI app, wired with config + auth + routers."""
    webapp_cfg = load_webapp_config()
    auth.ensure_auth_log_handler()

    app = FastAPI(title="Home Automation", version="0.1.0", lifespan=lifespan)

    # Read the token from app.state on every request so a future rotate
    # could take effect without re-importing the module.
    app.add_middleware(
        BearerTokenMiddleware,
        get_token=lambda: getattr(app.state.webapp_config, "auth_token", ""),
    )

    app.state.webapp_config = webapp_cfg

    if STATIC_DIR.exists():
        app.mount(
            "/static",
            CachingStaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(units.router)
    app.include_router(energy.router)
    app.include_router(weather.router)
    app.include_router(tuya.router)
    app.include_router(security.router)

    logger.info(
        "ℹ️  webapp build %s (fleet %s) built %s",
        BUILD_INFO.git_sha,
        BUILD_INFO.fleet_hash or "missing",
        BUILD_INFO.built_at,
    )
    logger.info(
        "ℹ️  webapp ready (auth gate %s)",
        "ON" if webapp_cfg.auth_token else "OFF",
    )
    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
