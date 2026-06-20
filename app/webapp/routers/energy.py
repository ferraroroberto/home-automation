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
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from src.energy_history import aggregate, framed_buckets, hourly_range, recent_samples
from src.sma_client import EnergyState, fetch_energy_state
from src.tariff import cost_breakdown, load_tariff

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


# Nominal day-count per window for prorating the fixed standing charge. ``total``
# is sized to the actual span of retained data instead (computed below).
_WINDOW_DAYS = {"day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0}


def _window_days(range_: str, buckets: List[Dict[str, Any]]) -> float:
    """Days the window spans, for prorating fixed costs."""
    if range_ in _WINDOW_DAYS:
        return _WINDOW_DAYS[range_]
    if not buckets:  # total, but no history yet
        return 0.0
    first = min(int(b["hour_start"]) for b in buckets)
    return max(1.0, (time.time() - first) / 86_400.0)


@router.get("/api/energy/cost")
async def get_energy_cost(
    range: str = Query("month", pattern="^(day|week|month|year|total)$"),
) -> Dict[str, Any]:
    """Tiered cost & savings breakdown for a window (issue #46).

    Splits the window's hourly energy into time-of-use periods (P1/P2/P3 for a
    Spanish 2.0TD tariff), prices grid import at each period's all-in rate, and
    values self-consumed PV at that same avoided rate (the savings). Returns
    per-period rows + totals + a fixed-cost / estimated-bill summary. Falls back
    to a flat 0.10 €/kWh estimate (``configured: false``) when no tariff is set.
    """
    try:
        buckets = hourly_range(range)
        tariff = load_tariff()
        result = cost_breakdown(buckets, tariff, _window_days(range, buckets))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to build energy cost breakdown: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to build cost: {exc}")
    result["range"] = range
    return result
