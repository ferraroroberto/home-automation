"""Live energy-flow API over the local SMA devices.

``GET /api/energy`` returns the home's instantaneous energy flow — grid
import/export from the Sunny Home Manager / energy meter, PV production from
the inverter (when awake), and the derived house consumption + PV surplus.
This is the read side of the eventual solar load-balancing automation.

Partial data is normal and returned with 200: the inverter sleeps at night,
so ``pv_power_w`` is ``null`` and ``inverter_reachable`` is ``false`` then.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from src.energy_history import aggregate, framed_buckets, recent_samples
from src.sma_client import EnergyState, fetch_energy_state

logger = logging.getLogger(__name__)

router = APIRouter()


def _energy_dict(s: EnergyState) -> Dict[str, Any]:
    """Flatten an :class:`EnergyState` into a JSON-serialisable dict."""
    return {
        "grid_import_w": s.grid_import_w,
        "grid_export_w": s.grid_export_w,
        "pv_power_w": s.pv_power_w,
        "house_consumption_w": s.house_consumption_w,
        "pv_surplus_w": s.pv_surplus_w,
        "grid_import_kwh": s.grid_import_kwh,
        "grid_export_kwh": s.grid_export_kwh,
        "meter_reachable": s.meter_reachable,
        "inverter_reachable": s.inverter_reachable,
        "meter_serial": s.meter_serial,
    }


@router.get("/api/energy")
async def get_energy() -> Dict[str, Any]:
    try:
        state = await fetch_energy_state()
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read energy flow: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read energy: {exc}")
    return _energy_dict(state)


@router.get("/api/energy/history")
async def get_energy_history(
    minutes: int = Query(60, ge=1, le=1440),
) -> Dict[str, Any]:
    """Recent raw samples for the live flowing chart.

    ``None`` powers (e.g. asleep PV) are preserved so the client draws a gap,
    never a misleading 0.
    """
    try:
        samples: List[Dict[str, Any]] = recent_samples(minutes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read energy history: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read history: {exc}")
    return {"minutes": minutes, "samples": samples}


@router.get("/api/energy/today")
async def get_energy_today() -> Dict[str, Any]:
    """Today's energy totals (one daily bucket) for the split + savings cards.

    Returns ``{"bucket": <daily bucket>}`` with ``pv_wh`` / ``house_wh`` /
    ``import_wh`` / ``export_wh`` / ``pv_missing`` for the current local day, or
    ``{"bucket": null}`` before any sample has landed today.
    """
    try:
        buckets = aggregate("daily", 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read today's energy: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read today: {exc}")
    return {"bucket": buckets[-1] if buckets else None}


@router.get("/api/energy/aggregate")
async def get_energy_aggregate(
    range: str = Query("day", pattern="^(day|week|month|year|total)$"),
) -> Dict[str, Any]:
    """Calendar-framed energy buckets (Wh) for the history chart.

    Each range is a fixed, fill-up window — 24h ``day``, Mon–Sun ``week``, the
    current ``month``, Jan–Dec ``year``, all-time ``total`` — carrying generation
    / grid-supplied / consumption energy per slot. Future slots come back empty,
    so the chart fills left-to-right. See :func:`framed_buckets`.
    """
    try:
        buckets = framed_buckets(range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to build energy history: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to aggregate: {exc}")
    return {"range": range, "buckets": buckets}
