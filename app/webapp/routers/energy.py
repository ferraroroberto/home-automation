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
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

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
