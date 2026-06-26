"""Local USB UPS status API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter

from src.ups_client import fetch_ups_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/ups")
async def get_ups() -> Dict[str, Any]:
    """Return local UPS telemetry from NUT or the Windows USB-HID battery driver."""
    state = await asyncio.to_thread(fetch_ups_state)
    return {"ups": state.to_dict()}
