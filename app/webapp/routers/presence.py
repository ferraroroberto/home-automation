"""Presence API over webhook state + cached iCloud diagnostics."""

from __future__ import annotations

import hmac
import logging
from dataclasses import asdict
from datetime import datetime, timezone
import os
from typing import Any, Dict

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

from app.webapp._env import _env_int
from app.webapp.presence_refresher import get_cache, refresh_once
from src.location_config import LocationConfig, load_location_config, save_location_config
from src.presence_client import PresenceEntity
from src.presence_display_names import (
    load_presence_display_names,
    set_presence_display_name,
)
from src.presence_engine import (
    PresenceAutomationConfig,
    load_automation_config,
    load_kids_home_override,
    load_people,
    now_utc,
    save_automation_config,
    set_kids_home_override,
    set_person_state,
)
from src.presence_hidden import load_hidden_presence_ids, set_presence_hidden

logger = logging.getLogger(__name__)

router = APIRouter()

_REVERSE_CACHE: Dict[str, Dict[str, Any]] = {}


def _entity_payload(entity: PresenceEntity, *, source: str = "icloud") -> Dict[str, Any]:
    payload = asdict(entity)
    last_seen = entity.last_seen
    payload["last_seen"] = last_seen.isoformat() if last_seen else None
    payload["source"] = source
    return payload


def _person_payload(person_id: str, *, now: datetime) -> Dict[str, Any]:
    people = load_people()
    names = load_presence_display_names()
    hidden = load_hidden_presence_ids()
    person = people[person_id]
    cfg = load_automation_config()
    age_s = (now - person.updated_at).total_seconds()
    at_home = person.state == "home"
    return {
        "entity_id": person_id,
        "name": person_id,
        "display_name": names.get(person_id) or None,
        "hidden": person_id in hidden,
        "model": None,
        "device_class": "Person",
        "latitude": None,
        "longitude": None,
        "horizontal_accuracy_m": None,
        "last_seen": person.updated_at.isoformat(),
        "battery_level_pct": None,
        "battery_status": None,
        "distance_from_home_m": 0.0 if at_home else None,
        "at_home": at_home,
        "state": person.state,
        "source": person.source,
        "stale": age_s > cfg.stale_after_s,
    }


def _presence_payload(entities: list[PresenceEntity]) -> Dict[str, Any]:
    now = now_utc()
    names = load_presence_display_names()
    hidden = load_hidden_presence_ids()
    diagnostic_entities = []
    for entity in entities:
        item = _entity_payload(entity, source="icloud")
        item["display_name"] = names.get(entity.entity_id) or None
        item["hidden"] = entity.entity_id in hidden
        item["stale"] = False
        diagnostic_entities.append(item)

    local_people = [_person_payload(pid, now=now) for pid in sorted(load_people())]
    all_entities = local_people + diagnostic_entities
    visible = [e for e in all_entities if not e.get("hidden")]
    located = [entity for entity in visible if entity.get("latitude") is not None and entity.get("longitude") is not None]
    at_home = [entity for entity in located if entity.get("at_home") is True]
    away = [entity for entity in located if entity.get("at_home") is False]
    local_home = [e for e in visible if e.get("source") == "webhook" and e.get("at_home") is True]
    local_away = [e for e in visible if e.get("source") == "webhook" and e.get("at_home") is False]
    unknown = [entity for entity in visible if entity.get("at_home") is None]
    cache = get_cache()
    return {
        "available": True,
        "entities": all_entities,
        "total_count": len(visible),
        "located_count": len(located),
        "home_count": len(local_home) + len(at_home),
        "away_count": len(local_away) + len(away),
        "unknown_count": len(unknown),
        "all_away": bool(visible) and not local_home and not at_home,
        "home_radius_m": cache.home_radius_m,
        "diagnostics": {
            "available": cache.available,
            "reason": cache.reason,
            "detail": cache.detail,
            "refreshed_at": cache.refreshed_at.isoformat() if cache.refreshed_at else None,
            "refresh_interval_s": max(60, _env_int("PRESENCE_ICLOUD_REFRESH_INTERVAL_S", 900)),
        },
        "automation": asdict(load_automation_config()),
        "kids_home_override": load_kids_home_override(),
    }


@router.get("/api/presence")
async def get_presence() -> Dict[str, Any]:
    """Return local presence state plus cached Find My diagnostics."""

    return _presence_payload(get_cache().entities)


def _webhook_secret() -> str:
    load_dotenv(override=True)
    return (os.getenv("PRESENCE_WEBHOOK_SECRET") or "").strip()


def _check_webhook_auth(request: Request) -> None:
    expected = _webhook_secret()
    if not expected:
        raise HTTPException(status_code=503, detail="PRESENCE_WEBHOOK_SECRET is not configured")
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    supplied = (
        bearer
        or request.headers.get("x-presence-secret", "").strip()
        or request.query_params.get("secret", "").strip()
    )
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid presence webhook secret")


class PresenceWebhookPayload(BaseModel):
    person_id: str
    state: str
    display_name: str = ""


@router.post("/api/presence/webhook")
async def post_presence_webhook(
    payload: PresenceWebhookPayload, request: Request
) -> Dict[str, Any]:
    _check_webhook_auth(request)
    person = set_person_state(payload.person_id, payload.state)
    if payload.display_name.strip():
        set_presence_display_name(payload.person_id, payload.display_name.strip())
    return {"ok": True, "person_id": person.person_id, "state": person.state, "updated_at": person.updated_at.isoformat()}


@router.post("/api/presence/webhooks/{person_id}/{state}")
async def post_presence_webhook_path(
    person_id: str, state: str, request: Request
) -> Dict[str, Any]:
    _check_webhook_auth(request)
    person = set_person_state(person_id, state)
    return {"ok": True, "person_id": person.person_id, "state": person.state, "updated_at": person.updated_at.isoformat()}


class PresenceDisplayNamePayload(BaseModel):
    entity_id: str
    display_name: str


@router.put("/api/presence/entity-display-name")
async def update_presence_display_name_safe(
    payload: PresenceDisplayNamePayload,
) -> Dict[str, Any]:
    """Set a display name for ids that are unsafe as URL path segments."""

    entity_id = payload.entity_id.strip()
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id is required")
    name = payload.display_name.strip()
    set_presence_display_name(entity_id, name)
    return {"entity_id": entity_id, "display_name": name or None}


class PresenceHiddenPayload(BaseModel):
    entity_id: str
    hidden: bool


@router.put("/api/presence/entity-hidden")
async def update_presence_hidden_safe(
    payload: PresenceHiddenPayload,
) -> Dict[str, Any]:
    """Set hidden flag for ids that are unsafe as URL path segments."""

    entity_id = payload.entity_id.strip()
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id is required")
    set_presence_hidden(entity_id, payload.hidden)
    return {"entity_id": entity_id, "hidden": payload.hidden}


@router.get("/api/presence/automation")
async def get_presence_automation() -> Dict[str, Any]:
    return asdict(load_automation_config())


class PresenceAutomationPayload(BaseModel):
    enabled: bool = False
    arm_away_after_s: int = 900
    stale_after_s: int = 3600
    disarm_on_arrival: bool = True


@router.put("/api/presence/automation")
async def update_presence_automation(payload: PresenceAutomationPayload) -> Dict[str, Any]:
    config = PresenceAutomationConfig(
        enabled=payload.enabled,
        arm_away_after_s=max(0, payload.arm_away_after_s),
        stale_after_s=max(60, payload.stale_after_s),
        disarm_on_arrival=payload.disarm_on_arrival,
    )
    save_automation_config(config)
    return asdict(config)


@router.get("/api/presence/kids_home_override")
async def get_kids_home_override() -> Dict[str, Any]:
    return {"active": load_kids_home_override()}


class KidsHomeOverridePayload(BaseModel):
    active: bool = False


@router.put("/api/presence/kids_home_override")
async def update_kids_home_override(payload: KidsHomeOverridePayload) -> Dict[str, Any]:
    set_kids_home_override(payload.active)
    return {"active": load_kids_home_override()}


@router.post("/api/presence/refresh")
async def post_presence_refresh() -> Dict[str, Any]:
    cache = await refresh_once()
    return {
        "available": cache.available,
        "reason": cache.reason,
        "detail": cache.detail,
        "refreshed_at": cache.refreshed_at.isoformat() if cache.refreshed_at else None,
    }


@router.get("/api/location")
async def get_location() -> Dict[str, Any]:
    location = load_location_config()
    if location is None:
        return {"configured": False, "lat": None, "lon": None, "label": ""}
    return {"configured": True, "lat": location.lat, "lon": location.lon, "label": location.label}


class LocationPayload(BaseModel):
    lat: float
    lon: float
    label: str = ""


@router.put("/api/location")
async def update_location(payload: LocationPayload) -> Dict[str, Any]:
    location = LocationConfig(lat=payload.lat, lon=payload.lon, label=payload.label.strip())
    try:
        save_location_config(location)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"configured": True, "lat": location.lat, "lon": location.lon, "label": location.label}


def _short_place(display_name: str) -> str:
    parts = [p.strip() for p in display_name.split(",") if p.strip()]
    return " · ".join(parts[:3]) if parts else display_name


@router.get("/api/location/reverse")
async def reverse_location(lat: float, lon: float) -> Dict[str, Any]:
    """Return a short place label for coordinates using OpenStreetMap Nominatim."""

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    key = f"{lat:.4f},{lon:.4f}"
    if key in _REVERSE_CACHE:
        return _REVERSE_CACHE[key]
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": "16"}
    headers = {"User-Agent": "home-automation-presence/0.1"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params, timeout=8) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Nominatim HTTP {resp.status}")
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Reverse geocode failed: %s", exc)
        return {"available": False, "label": "", "detail": str(exc)}
    label = _short_place(str(data.get("display_name") or ""))
    payload = {"available": bool(label), "label": label}
    _REVERSE_CACHE[key] = payload
    return payload
