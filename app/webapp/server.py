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

Run with::

    & .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447
    .\webapp.bat                                              # HTTPS when cert present
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import auth, energy, misc, tuya, units, weather
from app.webapp.routers._helpers import STATIC_DIR
from app.webapp.sampler import start_sampler
from src.webapp_config import load_webapp_config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the background energy sampler for the life of the webapp process."""
    task = start_sampler()
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
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
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(misc.router)
    app.include_router(auth.router)
    app.include_router(units.router)
    app.include_router(energy.router)
    app.include_router(weather.router)
    app.include_router(tuya.router)

    logger.info(
        "ℹ️  webapp ready (auth gate %s)",
        "ON" if webapp_cfg.auth_token else "OFF",
    )
    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
