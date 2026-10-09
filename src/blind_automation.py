"""Blind group control (issue #181).

UI-free core for moving several Tuya blinds at once — the house-wide
all-up / all-stop / all-down the Blinds card offers — following the
``src.hvac_automation`` split: decisions and fan-out live here, the thin
HTTP surface is ``app.webapp.routers.tuya``.

Every blind is a Maxcio-class Tuya curtain switch with exactly one DPS, an
``open/stop/close`` enum and no position feedback, so a group move is just the
per-device :func:`src.tuya_client.set_cover` sent to each blind. The commands
go out **in parallel** (each blocking TinyTuya call in its own worker thread)
rather than one after another, so the blinds start together instead of
rippling across the house one LAN round trip at a time.

One blind failing never stops the others: the result reports each device's
outcome so the caller can say exactly which blind did not move.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional

from src.tuya_client import (
    TuyaCommandError,
    TuyaConfigError,
    TuyaDeviceNotFoundError,
    list_devices,
    set_cover,
)

logger = logging.getLogger(__name__)

CoverAction = Literal["open", "close", "stop"]
COVER_ACTIONS: tuple[str, ...] = ("open", "close", "stop")


@dataclass(frozen=True)
class BlindOutcome:
    """One blind's result within a group move."""

    device_id: str
    ok: bool
    error: Optional[str] = None


def cover_device_ids() -> List[str]:
    """Every blind in ``devices.json``, deduplicated, in file order.

    A blind is any device with a cover-control DPS mapping — the same test
    ``GET /api/tuya`` uses for its ``has_cover`` flag.
    """
    seen: List[str] = []
    for info in list_devices():
        if info.cover_control_dps is not None and info.device_id and info.device_id not in seen:
            seen.append(info.device_id)
    return seen


async def move_blinds(
    action: CoverAction, device_ids: Optional[Iterable[str]] = None
) -> List[BlindOutcome]:
    """Send ``action`` to every blind in ``device_ids`` (default: all blinds).

    Raises :class:`ValueError` for an unknown action or a device id that is
    not a blind — a caller bug, distinct from a blind that is offline, which
    comes back as an ``ok=False`` outcome instead.
    """
    if action not in COVER_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(COVER_ACTIONS)}")
    known = cover_device_ids()
    targets = known if device_ids is None else list(dict.fromkeys(device_ids))
    unknown = [device_id for device_id in targets if device_id not in known]
    if unknown:
        raise ValueError(f"not a blind: {', '.join(unknown)}")

    async def _one(device_id: str) -> BlindOutcome:
        try:
            await asyncio.to_thread(set_cover, device_id, action)
        except (TuyaCommandError, TuyaConfigError, TuyaDeviceNotFoundError) as exc:
            return BlindOutcome(device_id=device_id, ok=False, error=str(exc))
        return BlindOutcome(device_id=device_id, ok=True)

    outcomes = list(await asyncio.gather(*(_one(device_id) for device_id in targets)))
    failed = [outcome.device_id for outcome in outcomes if not outcome.ok]
    if failed:
        logger.warning(
            "⚠️ Blind group %s: %d of %d blind(s) failed (%s)",
            action, len(failed), len(outcomes), ", ".join(failed),
        )
    else:
        logger.info("✅ Blind group %s sent to %d blind(s)", action, len(outcomes))
    return outcomes
