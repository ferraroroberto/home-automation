"""Bounded iCloud Find My refresher for Presence diagnostics.

Browser polling reads this module's in-memory cache through ``GET
/api/presence``. The expensive Apple call happens only in this background task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src.presence_client import (
    PresenceAuthError,
    PresenceConfigError,
    PresenceEntity,
    fetch_presence,
    load_presence_config,
)

logger = logging.getLogger(__name__)


@dataclass
class PresenceDiagnosticsCache:
    """Latest cached Find My diagnostic snapshot."""

    entities: list[PresenceEntity]
    refreshed_at: Optional[datetime] = None
    available: bool = False
    reason: str = "not_refreshed"
    detail: str = ""
    home_radius_m: Optional[float] = None


_CACHE = PresenceDiagnosticsCache(entities=[])


def get_cache() -> PresenceDiagnosticsCache:
    """Return the current diagnostics cache."""

    return _CACHE


async def refresh_once() -> PresenceDiagnosticsCache:
    """Fetch Find My once into the cache."""

    global _CACHE
    try:
        config = load_presence_config()
        entities = await asyncio.to_thread(lambda: fetch_presence(config=config))
        _CACHE = PresenceDiagnosticsCache(
            entities=entities,
            refreshed_at=datetime.now(timezone.utc),
            available=True,
            reason="ok",
            detail="",
            home_radius_m=config.home_radius_m,
        )
    except PresenceAuthError as exc:
        _CACHE = PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime.now(timezone.utc),
            available=False,
            reason="2fa_required",
            detail=str(exc),
        )
    except PresenceConfigError as exc:
        _CACHE = PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime.now(timezone.utc),
            available=False,
            reason="not_configured",
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to refresh iCloud presence diagnostics: %s", exc)
        _CACHE = PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime.now(timezone.utc),
            available=False,
            reason="error",
            detail=str(exc),
        )
    return _CACHE


async def _run(interval_s: int) -> None:
    await run_loop(
        refresh_once,
        interval_s,
        logger=logger,
        name="Presence diagnostics refresher",
        start_msg="📍 Presence diagnostics refresher started (interval %ds)" % interval_s,
    )


def start_presence_refresher() -> Optional[asyncio.Task]:
    """Start the bounded iCloud refresher unless disabled."""

    load_dotenv(override=True)
    if not _env_bool("PRESENCE_ICLOUD_REFRESH_ENABLED", True):
        logger.info("ℹ️ Presence diagnostics refresher disabled")
        return None
    interval_s = max(60, _env_int("PRESENCE_ICLOUD_REFRESH_INTERVAL_S", 900))
    return asyncio.create_task(_run(interval_s), name="presence-refresher")
