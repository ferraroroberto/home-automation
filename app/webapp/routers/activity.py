"""Unified activity / event log API over :mod:`src.telemetry` (#289).

``GET /api/activity`` reads back the unified ``events`` store — the queryable,
UI-surfaced successor to the write-only ``logs/*.jsonl`` trail and the otherwise
ephemeral RISCO event feed. Filtering is server-side: only the supplied facets
(``domain`` / ``type`` / ``since`` / ``limit``) narrow the parametrized query.
Read-only; events are written by the producers (alarm/power/presence/plug/RISCO),
not here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from src import telemetry

logger = logging.getLogger(__name__)

router = APIRouter()

# Cap a single page so a malicious/typo'd query can't pull the whole store.
_MAX_LIMIT = 500


@router.get("/api/activity")
async def get_activity(
    domain: Optional[str] = None,
    type: Optional[str] = None,
    since: Optional[int] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Return recent events, newest first, filtered by the supplied facets.

    ``domain`` (e.g. ``alarm`` / ``power`` / ``plug`` / ``security``) and
    ``type`` (the event_type) are exact-match; ``since`` is an epoch-second
    lower bound; ``limit`` is clamped to ``[1, 500]``.
    """
    safe_limit = max(1, min(int(limit or 100), _MAX_LIMIT))
    try:
        events: List[Dict[str, Any]] = await asyncio.to_thread(
            telemetry.read_events,
            domain=domain or None,
            event_type=type or None,
            since=since,
            limit=safe_limit,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean 500, don't leak internals
        logger.warning("⚠️  Failed to read activity events: %s", exc)
        raise HTTPException(status_code=500, detail="failed to read activity log")
    return {"events": events, "count": len(events)}


@router.get("/api/activity/domains")
async def get_activity_domains() -> Dict[str, Any]:
    """Distinct domains present in the store — populates the UI filter dropdown."""
    try:
        rows = await asyncio.to_thread(telemetry.read_events, limit=_MAX_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read activity domains: %s", exc)
        raise HTTPException(status_code=500, detail="failed to read activity log")
    domains = sorted({r["domain"] for r in rows if r.get("domain")})
    return {"domains": domains}


@router.get("/api/activity/readings")
async def get_readings(
    domain: Optional[str] = None,
    entity: Optional[str] = None,
    metric: Optional[str] = None,
    since: Optional[int] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return recent device readings, newest first, filtered by the supplied facets.

    Powers the Activity log's readings view (#290). ``value_num`` passes through
    unchanged — ``None`` (asleep) stays ``None``, never 0.
    """
    safe_limit = max(1, min(int(limit or 200), _MAX_LIMIT))
    try:
        readings: List[Dict[str, Any]] = await asyncio.to_thread(
            telemetry.read_readings,
            domain=domain or None,
            entity_id=entity or None,
            metric=metric or None,
            since=since,
            limit=safe_limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read telemetry readings: %s", exc)
        raise HTTPException(status_code=500, detail="failed to read telemetry readings")
    return {"readings": readings, "count": len(readings)}
