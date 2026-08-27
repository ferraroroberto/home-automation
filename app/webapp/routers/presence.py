"""Presence API over webhook state + cached iCloud diagnostics.

Presence/person CRUD + webhooks, named-places CRUD, automation-config CRUD,
and home-location config with Nominatim reverse-geocoding. Two feature panels
that used to live here now live in their own router modules (issue #702),
following the same split ``security.py`` (#346) and ``network.py`` (#328)
already use:

    ./presence_locate.py — the locate/ETA voice bridge (#438, #470)
    ./presence_trust.py  — the iCloud browser-trust renewal flow (#659)
"""

from __future__ import annotations

import hmac
import logging
from collections import OrderedDict
from dataclasses import asdict, replace
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
    load_arm_block,
    load_automation_config,
    load_kids_home_override,
    load_people,
    now_utc,
    save_automation_config,
    set_kids_home_override,
    set_person_state,
)
from src.presence_hidden import load_hidden_presence_ids, set_presence_hidden
from src.presence_places import (
    PresencePlace,
    load_presence_places,
    resolve_place,
    set_presence_places,
)
from src.presence_roles import load_presence_roles, set_presence_role

logger = logging.getLogger(__name__)

router = APIRouter()

# Nominatim answers are best-effort and the keyspace is open-ended (one entry
# per lat/lon rounded to ~11 m, so a phone in motion mints a new one every few
# seconds), while the webapp runs for weeks between tray restarts. Bounded
# LRU for the same reason `ha_trace_collector._SeenRuns` is bounded (#667);
# least-recently-used eviction keeps the household's usual places resident.
REVERSE_CACHE_LIMIT = 256
_REVERSE_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _entity_payload(
    entity: PresenceEntity, *, source: str = "icloud", places: list[PresencePlace] | None = None
) -> Dict[str, Any]:
    payload = asdict(entity)
    last_seen = entity.last_seen
    payload["last_seen"] = last_seen.isoformat() if last_seen else None
    payload["source"] = source
    payload["current_place"] = resolve_place(
        latitude=entity.latitude,
        longitude=entity.longitude,
        at_home=entity.at_home,
        has_location=entity.has_location,
        places=places or [],
    )
    return payload


_ICLOUD_STALE_AFTER_S_DEFAULT = 300


def _icloud_entity_stale(entity: PresenceEntity, *, now: datetime) -> bool:
    """An iCloud/Find My fix is stale once it's older than the threshold — a
    missing ``last_seen`` (no fix at all) is stale by definition (#483)."""

    if entity.last_seen is None:
        return True
    stale_after_s = max(0, _env_int("PRESENCE_ICLOUD_STALE_AFTER_S", _ICLOUD_STALE_AFTER_S_DEFAULT))
    return (now - entity.last_seen).total_seconds() > stale_after_s


def _person_payload(
    person_id: str, *, now: datetime, places: list[PresencePlace] | None = None
) -> Dict[str, Any]:
    people = load_people()
    names = load_presence_display_names()
    hidden = load_hidden_presence_ids()
    roles = load_presence_roles()
    person = people[person_id]
    cfg = load_automation_config()
    age_s = (now - person.updated_at).total_seconds()
    at_home = person.state == "home"
    return {
        "entity_id": person_id,
        "name": person_id,
        "display_name": names.get(person_id) or None,
        "role": roles.get(person_id) or None,
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
        "current_place": resolve_place(
            latitude=None,
            longitude=None,
            at_home=at_home,
            has_location=False,
            places=places or [],
        ),
    }


def _presence_payload(entities: list[PresenceEntity]) -> Dict[str, Any]:
    now = now_utc()
    names = load_presence_display_names()
    hidden = load_hidden_presence_ids()
    roles = load_presence_roles()
    places = load_presence_places()
    diagnostic_entities = []
    for entity in entities:
        item = _entity_payload(entity, source="icloud", places=places)
        item["display_name"] = names.get(entity.entity_id) or None
        item["role"] = roles.get(entity.entity_id) or None
        item["hidden"] = entity.entity_id in hidden
        item["stale"] = _icloud_entity_stale(entity, now=now)
        diagnostic_entities.append(item)

    local_people = [_person_payload(pid, now=now, places=places) for pid in sorted(load_people())]
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
            "accounts": [asdict(account) for account in cache.accounts],
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


class PresenceRolePayload(BaseModel):
    entity_id: str
    role: str


@router.put("/api/presence/role")
async def update_presence_role(payload: PresenceRolePayload) -> Dict[str, Any]:
    """Set or clear a household-role alias (e.g. "dad"/"mom") for voice lookup (#438)."""

    entity_id = payload.entity_id.strip()
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id is required")
    role = payload.role.strip()
    set_presence_role(entity_id, role)
    return {"entity_id": entity_id, "role": role or None}


def _places_payload(places: list[PresencePlace]) -> Dict[str, Any]:
    return {"places": [asdict(place) for place in places], "count": len(places)}


@router.get("/api/presence/places")
async def get_presence_places() -> Dict[str, Any]:
    """Return the configured named places for the locator (#438)."""

    return _places_payload(load_presence_places())


@router.put("/api/presence/places")
async def update_presence_places(request: Request) -> Dict[str, Any]:
    """Replace the whole named-place list (browser-managed dense collection)."""

    body = await request.json()
    entries = body.get("places") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="places must be a list")
    return _places_payload(set_presence_places(entries))


# The locate/ETA voice bridge (issue #438, #470) lives in ./presence_locate.py
# (issue #702) — it shares its own resolution/staleness machinery and touches
# none of the webhook/CRUD state below.


@router.get("/api/presence/automation")
async def get_presence_automation() -> Dict[str, Any]:
    payload = asdict(load_automation_config())
    block = load_arm_block()
    payload["arm_blocked"] = block["blocked"]
    payload["arm_blocked_person_ids"] = block["person_ids"]
    payload["arm_blocked_since"] = block["since"]
    return payload


class PresenceAutomationPayload(BaseModel):
    auto_arm_enabled: bool = False
    arm_away_after_s: int = 900
    stale_after_s: int = 3600
    auto_disarm_enabled: bool = False


@router.put("/api/presence/automation")
async def update_presence_automation(payload: PresenceAutomationPayload) -> Dict[str, Any]:
    # Rebuild from the persisted config rather than from bare defaults, so the
    # fields the PWA form does not carry (`arm_action`, `disarm_action`, and the
    # #598 `disarm_max_age_s` safety bound) survive a save instead of being
    # silently reset every time a toggle is flipped.
    config = replace(
        load_automation_config(),
        auto_arm_enabled=payload.auto_arm_enabled,
        arm_away_after_s=max(0, payload.arm_away_after_s),
        stale_after_s=max(60, payload.stale_after_s),
        auto_disarm_enabled=payload.auto_disarm_enabled,
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
        "accounts": [asdict(account) for account in cache.accounts],
        "refreshed_at": cache.refreshed_at.isoformat() if cache.refreshed_at else None,
    }


# The iCloud browser-trust renewal flow (issue #659) lives in
# ./presence_trust.py (issue #702) — fully self-contained, shares no state
# with the rest of this router beyond triggering a diagnostics refresh.


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


async def _reverse_geocode(lat: float, lon: float) -> Dict[str, Any]:
    """Look up (and cache) a short place label for coordinates via Nominatim.

    Shared by the browser-facing ``/api/location/reverse`` endpoint and the
    voice-bridge locate resolution (#442) — same cache, so a coordinate already
    looked up for the Presence card's place label costs nothing to reuse for
    speech.
    """

    key = f"{lat:.4f},{lon:.4f}"
    if key in _REVERSE_CACHE:
        _REVERSE_CACHE.move_to_end(key)
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
    _REVERSE_CACHE.move_to_end(key)
    while len(_REVERSE_CACHE) > REVERSE_CACHE_LIMIT:
        _REVERSE_CACHE.popitem(last=False)
    return payload


@router.get("/api/location/reverse")
async def reverse_location(lat: float, lon: float) -> Dict[str, Any]:
    """Return a short place label for coordinates using OpenStreetMap Nominatim."""

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    return await _reverse_geocode(lat, lon)
