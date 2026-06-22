"""Presence API over the read-only iCloud Find My spike client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import APIRouter

from src.presence_client import (
    PresenceConfig,
    PresenceAuthError,
    PresenceConfigError,
    PresenceEntity,
    fetch_presence,
    load_presence_config,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _entity_payload(entity: PresenceEntity) -> Dict[str, Any]:
    payload = asdict(entity)
    last_seen = entity.last_seen
    payload["last_seen"] = last_seen.isoformat() if last_seen else None
    return payload


def _presence_payload(
    entities: list[PresenceEntity], config: PresenceConfig
) -> Dict[str, Any]:
    located = [entity for entity in entities if entity.has_location]
    at_home = [entity for entity in located if entity.at_home is True]
    away = [entity for entity in located if entity.at_home is False]
    unknown = [entity for entity in entities if entity.at_home is None]
    return {
        "available": True,
        "entities": [_entity_payload(entity) for entity in entities],
        "total_count": len(entities),
        "located_count": len(located),
        "home_count": len(at_home),
        "away_count": len(away),
        "unknown_count": len(unknown),
        "all_away": bool(located) and not at_home,
        "home_radius_m": config.home_radius_m,
    }


@router.get("/api/presence")
async def get_presence() -> Dict[str, Any]:
    """Return the current iCloud Find My presence snapshot.

    Presence is an optional spike dependency, so config/auth failures return a
    structured unavailable payload instead of blacking out the Security tab.
    """

    try:
        config = load_presence_config()
        return await asyncio.to_thread(
            lambda: _presence_payload(fetch_presence(config=config), config)
        )
    except PresenceAuthError as exc:
        return {"available": False, "reason": "2fa_required", "detail": str(exc)}
    except PresenceConfigError as exc:
        return {"available": False, "reason": "not_configured", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to read iCloud presence: %s", exc)
        return {"available": False, "reason": "error", "detail": str(exc)}
