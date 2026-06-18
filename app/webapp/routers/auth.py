"""Password → bearer-token login, plus the dedicated auth-attempt log."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.webapp.routers._helpers import PROJECT_ROOT
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# Dedicated logger for password attempts — written to webapp/auth.log so
# failed attempts are easy to find without scrolling the full server log.
auth_logger = logging.getLogger("home_automation.auth")
_AUTH_LOG_PATH = PROJECT_ROOT / "webapp" / "auth.log"


def ensure_auth_log_handler() -> None:
    """Attach the webapp/auth.log file handler to ``auth_logger`` once."""
    if any(
        isinstance(h, logging.FileHandler)
        and Path(h.baseFilename).resolve() == _AUTH_LOG_PATH.resolve()
        for h in auth_logger.handlers
    ):
        return
    try:
        _AUTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_AUTH_LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        auth_logger.addHandler(fh)
        auth_logger.setLevel(logging.INFO)
    except OSError as exc:
        logger.warning("⚠️  Could not open %s: %s", _AUTH_LOG_PATH, exc)


async def _maybe_json(request: Request) -> Dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


@router.post("/api/login")
async def login(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    client_host = request.client.host if request.client else "?"
    if not cfg.auth_password:
        auth_logger.info(
            "⚠️  Login attempt from %s but no auth_password configured", client_host
        )
        raise HTTPException(status_code=503, detail="password auth not configured")
    if not cfg.auth_token:
        auth_logger.info(
            "⚠️  Login attempt from %s but no auth_token configured", client_host
        )
        raise HTTPException(status_code=503, detail="bearer token not configured")
    body = await _maybe_json(request)
    presented = str(body.get("password") or "")
    if not presented or not hmac.compare_digest(presented, cfg.auth_password):
        auth_logger.warning(
            "🚨 Failed password attempt from %s (presented %d chars)",
            client_host, len(presented),
        )
        raise HTTPException(status_code=401, detail="bad password")
    auth_logger.info("🔓 Password login from %s", client_host)
    return {"token": cfg.auth_token}
