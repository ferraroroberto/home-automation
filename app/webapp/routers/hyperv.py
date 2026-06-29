"""Home Assistant Hyper-V VM status + control (issue #240).

``GET /api/hyperv`` returns one flattened :class:`~src.hyperv_client.HyperVState`
(name, running/off, uptime, IP, MAC). Only a missing ``HA_VM_NAME`` is a 503;
every other read problem (VM off, not found, unprivileged read) rides in the body
so the Home card can render a useful message — the network/UPS "partial data
stays 200" idiom.

``POST /api/hyperv/{start|stop}`` powers the VM on / gracefully shuts it down and
reads the state back (the ``units.py`` write-then-read idiom). Each cause maps to
its own status: missing name → 503, VM not found → 404, insufficient Hyper-V
rights → 403, already in that state → 409, bad action → 400, anything else → 502.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict

from fastapi import APIRouter, HTTPException

from src.hyperv_client import (
    HyperVCommandError,
    HyperVConfigError,
    HyperVNotFoundError,
    HyperVPermissionError,
    HyperVState,
    HyperVStateError,
    fetch_hyperv_state,
    start_vm,
    stop_vm,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/hyperv")
async def get_hyperv() -> Dict[str, Any]:
    """Return the Home Assistant VM's live status."""
    try:
        state = await asyncio.to_thread(fetch_hyperv_state)
    except HyperVConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"hyperv": state.to_dict()}


_ACTIONS: Dict[str, Callable[[], HyperVState]] = {"start": start_vm, "stop": stop_vm}


@router.post("/api/hyperv/{action}")
async def control_hyperv(action: str) -> Dict[str, Any]:
    """Start or gracefully stop the VM (the UI gates Stop behind a confirm)."""
    func = _ACTIONS.get(action)
    if func is None:
        raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")
    try:
        state = await asyncio.to_thread(func)
    except HyperVConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except HyperVNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except HyperVPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except HyperVStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except HyperVCommandError as exc:
        logger.warning("⚠️  Hyper-V %s failed: %s", action, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {"hyperv": state.to_dict()}
