r"""FastAPI webapp — mobile-first MELCloud Home control dashboard.

Routes (split across ``app/webapp/routers/``):

    GET  /                       → static/index.html        (misc)
    GET  /static/{file}          → CSS / JS / icons          (static mount)
    GET  /healthz                → liveness probe            (misc)
    GET  /install-ca             → iOS .mobileconfig         (misc)
    POST /api/login              → password → bearer token   (auth)
    GET  /api/units              → live state of every unit  (units)
    POST /api/units/{id}         → write controls + read back (units)

Run with::

    & .\.venv\Scripts\python.exe -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447
    .\webapp.bat                                              # HTTPS when cert present
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.webapp.middleware import BearerTokenMiddleware
from app.webapp.routers import auth, misc, units
from app.webapp.routers._helpers import STATIC_DIR
from src.webapp_config import load_webapp_config

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI app, wired with config + auth + routers."""
    webapp_cfg = load_webapp_config()
    auth.ensure_auth_log_handler()

    app = FastAPI(title="Home Automation", version="0.1.0")

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

    logger.info(
        "ℹ️  webapp ready (auth gate %s)",
        "ON" if webapp_cfg.auth_token else "OFF",
    )
    return app


# Module-level app for `uvicorn app.webapp.server:app`.
app = create_app()
