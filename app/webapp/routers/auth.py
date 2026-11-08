"""Password → bearer-token login, plus the dedicated auth-attempt log."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.webapp.middleware import is_pc_itself
from src.login_lockout import LockoutAlert, LoginLockout, retry_after_header
from src.notify import Notifier, NotifierError
from src.notify_config import build_attention_notifier
from src.secret_compare import secrets_match
from src.webapp_config import WebappConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# Dedicated logger for password attempts — written to webapp/auth.log so
# failed attempts are easy to find without scrolling the full server log.
auth_logger = logging.getLogger("home_automation.auth")
_AUTH_LOG_PATH = Path(__file__).resolve().parents[3] / "webapp" / "auth.log"


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


def make_login_alert(
    notifier_factory: Callable[[], Optional[Notifier]] = build_attention_notifier,
) -> Callable[[LockoutAlert], None]:
    """The lock-out's ``on_alert``: one message to the ``coding alerts`` chat.

    The text names the app, the client address and the failure count, never a
    password or token. The send runs in a thread so the login never waits on Telegram.
    """

    def alert_owner(alert: LockoutAlert) -> None:
        held = f" (+{alert.suppressed} more held back)" if alert.suppressed else ""
        text = (
            f"🚨 home-automation: {alert.failures} wrong passwords in a row "
            f"from {alert.key}{held}"
        )

        def send() -> None:
            notifier = notifier_factory()
            if notifier is None:
                logger.info("ℹ️ login alert not sent: notifier not configured")
                return
            try:
                notifier.send_text(text)
            except NotifierError as exc:
                logger.warning("⚠️ login alert not delivered: %s", type(exc).__name__)

        threading.Thread(target=send, daemon=True).start()

    return alert_owner


def build_login_lockout() -> LoginLockout:
    """One lock-out per app, stored on ``app.state`` by ``create_app``."""
    return LoginLockout(on_alert=make_login_alert())


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
    lockout: LoginLockout = request.app.state.login_lockout
    exempt = is_pc_itself(request)
    wait = 0.0 if exempt else lockout.retry_after(client_host)
    if wait > 0:
        auth_logger.warning("🔒 Login from %s refused: locked out", client_host)
        raise HTTPException(
            status_code=429, detail="too many attempts", headers=retry_after_header(wait)
        )
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
    if not presented or not secrets_match(presented, cfg.auth_password):
        auth_logger.warning(
            "🚨 Failed password attempt from %s (presented %d chars)",
            client_host, len(presented),
        )
        if not exempt:
            lockout.record_failure(client_host)
        raise HTTPException(status_code=401, detail="bad password")
    lockout.reset(client_host)
    auth_logger.info("🔓 Password login from %s", client_host)
    return {"token": cfg.auth_token}
