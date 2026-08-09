"""Generalized action alias — ``POST /api/actions/{action_id}`` (issue #641).

One stable surface over the existing per-device-family endpoints so an
external trigger (a Stream Deck button today, anything else later) needs to
know only an ``action_id``, not each domain's own path/id shape. Looks the id
up in ``app.webapp.actions_registry.ACTIONS``, runs the corresponding
existing call, and records one ``action``-domain telemetry event tagged with
the caller so these are distinguishable from webapp-UI-triggered calls to the
same underlying devices (e.g. a tap on the Security tab, which goes through
``POST /api/security/{action}`` directly and is never tagged here).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Request

from app.webapp.actions_registry import ACTIONS
from src import telemetry
from src.ha_client import HaClientError
from src.risco_client import RiscoCommandError, RiscoConfigError
from src.tuya_client import TuyaCommandError, TuyaConfigError, TuyaDeviceNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter()

# Any external caller identifies itself via this header (issue #405 precedent
# on the security router); unlike that router's fixed ha/voice-pe allowlist,
# this endpoint is meant for arbitrary future external triggers, so any
# short token is accepted verbatim rather than collapsed to a known set.
_AUTOMATION_SOURCE_HEADER = "x-automation-source"
_MAX_ACTOR_LEN = 32


def _actor_from_request(request: Request) -> str:
    raw = (request.headers.get(_AUTOMATION_SOURCE_HEADER) or "").strip().lower()
    return raw[:_MAX_ACTOR_LEN] if raw else "external"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (RiscoConfigError, TuyaConfigError)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TuyaDeviceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, HaClientError):
        return HTTPException(status_code=exc.status, detail=str(exc))
    if isinstance(exc, (RiscoCommandError, TuyaCommandError)):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@asynccontextmanager
async def _http_session(request: Request):
    """Reuse the lifespan-owned pool; create a temporary one outside a lifespan."""

    shared = getattr(request.app.state, "outbound_http", None)
    if shared is not None and not shared.closed:
        yield shared
        return
    async with aiohttp.ClientSession() as session:
        yield session


def _record_action_event(action_id: str, actor: str, outcome: str, error: Optional[str]) -> None:
    try:
        telemetry.record_event(
            "action",
            action_id,
            entity_id=action_id,
            source=actor,
            outcome=outcome,
            payload={"error": error} if error else None,
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        logger.debug("telemetry action event skipped", exc_info=True)


@router.post("/api/actions/{action_id}")
async def post_action(action_id: str, request: Request) -> Dict[str, Any]:
    handler = ACTIONS.get(action_id)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown action '{action_id}'")

    actor = _actor_from_request(request)
    try:
        async with _http_session(request) as session:
            result = await handler(actor, session)
    except Exception as exc:  # noqa: BLE001 — mapped to a clean HTTP error below
        _record_action_event(action_id, actor, "error", str(exc))
        if isinstance(exc, HTTPException):
            raise
        raise _http_error(exc)

    _record_action_event(action_id, actor, "ok", None)
    return {"action_id": action_id, "ok": True, **result}
