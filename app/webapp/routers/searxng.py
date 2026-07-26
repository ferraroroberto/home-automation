"""SearXNG status + start control (issue #321).

``GET /api/searxng`` returns one flattened :class:`~src.searxng_client.SearxngState`
(container status, ``/healthz`` reachability, url). Any read problem (container
not found, stopped, or up-but-unreachable) rides in the body so the Home card
can render a useful message — the network/UPS "partial data stays 200" idiom.

``POST /api/searxng/start`` runs ``docker compose up -d`` (idempotent) and
reads the state back (the ``units.py`` write-then-read idiom). A missing
``SEARXNG_COMPOSE_PATH`` is a 503; any other start failure is a 502.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.searxng_client import (
    SearxngCommandError,
    SearxngConfigError,
    fetch_searxng_state,
    start_searxng,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/searxng")
async def get_searxng() -> Dict[str, Any]:
    """Return the SearXNG container's live status."""
    state = await asyncio.to_thread(fetch_searxng_state)
    return {"searxng": state.to_dict()}


@router.post("/api/searxng/start")
async def control_searxng_start() -> Dict[str, Any]:
    """Bring the SearXNG container up (idempotent) and return the read-back state."""
    try:
        state = await asyncio.to_thread(start_searxng)
    except SearxngConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except SearxngCommandError as exc:
        logger.warning("⚠️  SearXNG start failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {"searxng": state.to_dict()}
