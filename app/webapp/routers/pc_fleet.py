"""UPS-triggered PC-fleet shutdown preferences + hub proxy routes (#498).

``GET/PUT /api/pc-fleet/prefs`` reads/writes :mod:`src.pc_fleet_prefs`
(enabled, threshold_minutes, excluded) — the whole-object shape, merged
against the currently-saved values for any keys the caller omits (same
partial-tolerant idiom as ``ups.py``'s notify-prefs route). Clamping of
``threshold_minutes`` to 1..240 happens inside
:func:`src.pc_fleet_prefs.save_pc_fleet_prefs`; this router just re-reads the
saved file afterwards so the response always reflects what actually landed
on disk.

``GET /api/pc-fleet/machines`` and ``POST /api/pc-fleet/wake/{host_id}`` are
thin proxies onto the local-llm-hub admin API (``PC_FLEET_HUB_BASE``, default
``http://127.0.0.1:8000``) — loopback calls that need no auth token. The
machines list is passed through verbatim on a 200; the wake proxy passes
through the hub's status code and body as-is (200/400/404/502 are all
meaningful to the UI). Any connect error, timeout, or otherwise-unreachable
hub collapses to a clean ``502`` JSON body — never a stacktrace.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.pc_fleet_prefs import (
    PcFleetPrefs,
    load_pc_fleet_prefs,
    save_pc_fleet_prefs,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_HUB_TIMEOUT_SECONDS = 5.0


def _hub_base() -> str:
    return os.environ.get("PC_FLEET_HUB_BASE", "http://127.0.0.1:8000").rstrip("/")


async def _hub_get(url: str) -> httpx.Response:
    """Thin wrapper so tests can monkeypatch the actual network call."""
    async with httpx.AsyncClient(timeout=_HUB_TIMEOUT_SECONDS) as http_client:
        return await http_client.get(url)


async def _hub_post(url: str) -> httpx.Response:
    """Thin wrapper so tests can monkeypatch the actual network call."""
    async with httpx.AsyncClient(timeout=_HUB_TIMEOUT_SECONDS) as http_client:
        return await http_client.post(url)


def _prefs_payload(prefs: PcFleetPrefs) -> Dict[str, Any]:
    return {
        "enabled": prefs.enabled,
        "threshold_minutes": prefs.threshold_minutes,
        "excluded": list(prefs.excluded),
    }


@router.get("/api/pc-fleet/prefs")
async def get_pc_fleet_prefs() -> Dict[str, Any]:
    """Return the fleet-shutdown prefs (enabled, threshold, excluded hosts)."""
    return _prefs_payload(load_pc_fleet_prefs())


@router.put("/api/pc-fleet/prefs")
async def update_pc_fleet_prefs(request: Request) -> Dict[str, Any]:
    """Accept the whole prefs object, persist it, and echo back what was saved."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    current = load_pc_fleet_prefs()
    excluded = body.get("excluded", current.excluded)
    if not isinstance(excluded, (list, tuple)):
        excluded = current.excluded

    prefs = PcFleetPrefs(
        enabled=bool(body.get("enabled", current.enabled)),
        threshold_minutes=body.get("threshold_minutes", current.threshold_minutes),
        excluded=tuple(str(host) for host in excluded if host),
    )
    try:
        save_pc_fleet_prefs(prefs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to save pc-fleet prefs: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to save pc-fleet prefs: {exc}")

    return _prefs_payload(load_pc_fleet_prefs())


@router.get("/api/pc-fleet/machines")
async def get_pc_fleet_machines() -> Dict[str, Any]:
    """Proxy the hub's machine-status list verbatim (loopback, no auth token)."""
    url = f"{_hub_base()}/admin/api/machines/status"
    try:
        resp = await _hub_get(url)
    except httpx.HTTPError as exc:
        logger.warning("⚠️  Hub unreachable for machine status (%s): %s", url, exc)
        raise HTTPException(status_code=502, detail="hub unreachable")
    if resp.status_code != 200:
        logger.warning(
            "⚠️  Hub returned %s for machine status (%s)", resp.status_code, url
        )
        raise HTTPException(status_code=502, detail="hub unreachable")
    return resp.json()


@router.post("/api/pc-fleet/wake/{host_id}")
async def wake_pc_fleet_machine(host_id: str) -> JSONResponse:
    """Proxy a wake-on-LAN request, passing through the hub's status + body."""
    url = f"{_hub_base()}/admin/api/machines/{host_id}/wake"
    try:
        resp = await _hub_post(url)
    except httpx.HTTPError as exc:
        logger.warning("⚠️  Hub unreachable for wake (%s): %s", url, exc)
        raise HTTPException(status_code=502, detail="hub unreachable")
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": "hub returned a non-JSON response"}
    return JSONResponse(status_code=resp.status_code, content=payload)
