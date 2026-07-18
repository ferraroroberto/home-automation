"""Presence API over webhook state + cached iCloud diagnostics."""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import asdict
from datetime import datetime, timezone
import os
from typing import Any, Dict, Optional, Tuple

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

from app.webapp._env import _env_int
from app.webapp.presence_refresher import PresenceDiagnosticsCache, get_cache, refresh_once
from src.activity_log import append_activity
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
from src.presence_places import (
    UNKNOWN_PLACE,
    PresencePlace,
    load_presence_places,
    resolve_place,
    set_presence_places,
)
from src.presence_roles import load_presence_roles, resolve_person, set_presence_role
from src.travel_time import fetch_travel_time

logger = logging.getLogger(__name__)

router = APIRouter()

_REVERSE_CACHE: Dict[str, Dict[str, Any]] = {}


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
        item["stale"] = False
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


_LOCATE_STALE_AFTER_S_DEFAULT = 120
_LOCATE_REFRESH_TIMEOUT_S_DEFAULT = 5


def _cache_is_stale(cache: PresenceDiagnosticsCache, *, now: datetime) -> bool:
    if cache.refreshed_at is None:
        return True
    stale_after_s = max(0, _env_int("PRESENCE_LOCATE_STALE_AFTER_S", _LOCATE_STALE_AFTER_S_DEFAULT))
    return (now - cache.refreshed_at).total_seconds() > stale_after_s


async def _cache_for_locate(*, now: datetime) -> PresenceDiagnosticsCache:
    """Cached by default; a bounded on-demand refresh when the cache is stale (#442).

    A locate query is user-initiated and rare, so one extra Apple round-trip per
    question is acceptable and doesn't change the background refresh cadence
    (``GET /api/presence`` still reads the cache as-is, no refresh triggered).
    """

    cache = get_cache()
    if not _cache_is_stale(cache, now=now):
        return cache
    timeout_s = max(1, _env_int("PRESENCE_LOCATE_REFRESH_TIMEOUT_S", _LOCATE_REFRESH_TIMEOUT_S_DEFAULT))
    try:
        return await asyncio.wait_for(refresh_once(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning(
            "⚠️ On-demand presence refresh timed out after %ds; using cached snapshot", timeout_s
        )
        return get_cache()


def _locate_lang(lang: str) -> str:
    """Clamp the voice-bridge language hint to the two served pipelines (#446)."""

    return "es" if lang.strip().lower().startswith("es") else "en"


def _place_speech(display_name: str, place: str, *, lang: str = "en") -> str:
    if lang == "es":
        if place == "Home":
            return f"{display_name} está en casa."
        if place in ("Away", UNKNOWN_PLACE):
            return f"{display_name} está fuera — no sé exactamente dónde."
        return f"{display_name} está en {place}."
    if place == "Home":
        return f"{display_name} is home."
    if place in ("Away", UNKNOWN_PLACE):
        return f"{display_name} is away — I don't know exactly where."
    return f"{display_name} is at {place}."


def _broken_source_speech(
    display_name: str, cache: PresenceDiagnosticsCache, *, lang: str = "en"
) -> str:
    """Speech for a person known only via a role/display-name alias that isn't in
    either live source right now — normally an iCloud-only entity whose Find My
    refresh is currently failing, not a person who is just "away" (#442)."""

    if lang == "es":
        if cache.reason == "2fa_required":
            return f"El localizador de {display_name} necesita re-autenticación de iCloud."
        if cache.reason in ("error", "not_configured"):
            return f"El localizador de {display_name} no funciona — necesita re-autenticación."
        return f"No encuentro la ubicación de {display_name} ahora mismo."
    if cache.reason == "2fa_required":
        return f"{display_name}'s location tracking needs iCloud re-authentication."
    if cache.reason in ("error", "not_configured"):
        return f"{display_name}'s location tracking is down — needs re-authentication."
    return f"I can't find {display_name}'s location right now."


async def _resolved_away_place(entity: PresenceEntity, place: str) -> str:
    """Reverse-geocode a located-but-unmatched-place entity instead of a bare
    "Away" (#442) — e.g. "Gran Via, Barcelona", mirroring the reverse-geocoded
    label the Presence card already shows for the same coordinates. Only
    "Away" (real coordinates, no configured place matched) is eligible; a
    named-place match or a missing location passes through unchanged."""

    if place != "Away" or entity.latitude is None or entity.longitude is None:
        return place
    result = await _reverse_geocode(entity.latitude, entity.longitude)
    label = result.get("label") if isinstance(result, dict) else ""
    return label or place


def _resolve_presence_target(
    who: str, cache: PresenceDiagnosticsCache
) -> Tuple[Optional[str], Optional[str], Dict[str, PresenceEntity], Dict[str, Any]]:
    """Resolve a spoken name/role to ``(entity_id, display_name, icloud, people)``.

    Shared by the locate (#438) and ETA (#470) voice bridges — both fold the
    spoken ``who`` through role aliases / display names / raw names identically.
    The two lookup maps ride along so the caller reads coordinates/state without
    rebuilding them; ``entity_id`` / ``display_name`` are ``None`` when unmatched.
    """

    roles = load_presence_roles()
    names = load_presence_display_names()
    icloud_entities = {entity.entity_id: entity for entity in cache.entities}
    people = load_people()
    known_ids = list(people) + list(icloud_entities)
    known_names = {eid: entity.name for eid, entity in icloud_entities.items()}
    entity_id = resolve_person(
        who,
        roles=roles,
        display_names=names,
        known_ids=known_ids,
        known_names=known_names,
    )
    display_name = (
        (names.get(entity_id) or known_names.get(entity_id) or entity_id)
        if entity_id is not None
        else None
    )
    return entity_id, display_name, icloud_entities, people


@router.get("/api/presence/locate")
async def get_presence_locate(who: str, lang: str = "en") -> Dict[str, Any]:
    """Resolve a spoken name/role to a current place — the voice-bridge endpoint (#438).

    Reads the background diagnostics refresher's cache, same as ``GET
    /api/presence``, but refreshes on demand when that cache is stale (#442).
    ``lang=es`` makes the ready-made ``speech`` Spanish for the "Hey Mycroft"
    pipeline (#446); resolution itself is language-agnostic.
    """

    lang = _locate_lang(lang)
    now = now_utc()
    cache = await _cache_for_locate(now=now)
    places = load_presence_places()

    entity_id, display_name, icloud_entities, people = _resolve_presence_target(who, cache)
    if entity_id is None:
        not_found_speech = (
            f"No sé quién es {who.strip()}."
            if lang == "es"
            else f"I don't know who {who.strip()} is."
        )
        result = {
            "found": False,
            "entity_id": None,
            "name": who.strip(),
            "place": None,
            "speech": not_found_speech,
        }
        append_activity("presence_locate", {"who": who, "lang": lang, **result})
        return result

    if entity_id in icloud_entities:
        entity = icloud_entities[entity_id]
        place = resolve_place(
            latitude=entity.latitude,
            longitude=entity.longitude,
            at_home=entity.at_home,
            has_location=entity.has_location,
            places=places,
        )
        place = await _resolved_away_place(entity, place)
        speech = _place_speech(display_name, place, lang=lang)
    elif entity_id in people:
        person = people[entity_id]
        place = resolve_place(
            latitude=None,
            longitude=None,
            at_home=person.state == "home",
            has_location=False,
            places=places,
        )
        speech = _place_speech(display_name, place, lang=lang)
    else:
        # Known via a role/display-name alias but absent from both live sources
        # right now — the Find My cache is empty because the diagnostics
        # refresher is down, not because this person is simply "away" (#442).
        place = UNKNOWN_PLACE
        speech = _broken_source_speech(display_name, cache, lang=lang)

    result = {"found": True, "entity_id": entity_id, "name": display_name, "place": place, "speech": speech}
    append_activity("presence_locate", {"who": who, "lang": lang, **result})
    return result


def _duration_phrase(minutes: int, *, lang: str) -> str:
    """The spoken duration, complete for its language (#474).

    Under an hour, bare minutes; an hour or more, "H hour(s) [M minute(s)]"
    dropping the minutes when zero. Spanish folds "about" into the leading unit's
    article ("unos"/"unas"), and the singular is the number word itself — "un
    minuto", "una hora" (never "un 1 minuto") — with gender agreement (*minutos*
    masculine, *horas* feminine). English keeps the numeral and gets its "about"
    from the caller's sentence frame.
    """

    hours, mins = divmod(minutes, 60)
    if lang == "es":
        if hours == 0:
            return "un minuto" if mins == 1 else f"unos {mins} minutos"
        head = "una hora" if hours == 1 else f"unas {hours} horas"
        if mins == 0:
            return head
        tail = "un minuto" if mins == 1 else f"{mins} minutos"
        return f"{head} y {tail}"
    if hours == 0:
        return "1 minute" if mins == 1 else f"{mins} minutes"
    head = "1 hour" if hours == 1 else f"{hours} hours"
    if mins == 0:
        return head
    tail = "1 minute" if mins == 1 else f"{mins} minutes"
    return f"{head} {tail}"


def _eta_speech(display_name: str, duration_s: int, *, lang: str = "en") -> str:
    """Spoken traffic-aware ETA (#470) — the app owns the wording; minutes round
    up so a sub-minute hop still reads as "about 1 minute", and durations of an
    hour or more are spoken as hours + minutes rather than raw minutes (#474)."""

    minutes = max(1, round(duration_s / 60))
    phrase = _duration_phrase(minutes, lang=lang)
    if lang == "es":
        return f"{display_name} está a {phrase} de casa con el tráfico actual."
    return f"{display_name} is about {phrase} from home in current traffic."


def _eta_unavailable_speech(display_name: str, reason: str, *, lang: str = "en") -> str:
    """Spoken fallback when no ETA could be computed — mirrors the locator's
    graceful "can't find location" wording rather than surfacing an error."""

    if lang == "es":
        if reason == "no_api_key":
            return "El cálculo de trayecto no está configurado."
        if reason == "no_route":
            return f"No encuentro una ruta a casa desde donde está {display_name}."
        return f"No puedo calcular cuánto tardará {display_name} en llegar a casa ahora mismo."
    if reason == "no_api_key":
        return "Travel-time lookup isn't set up."
    if reason == "no_route":
        return f"I can't find a route home from where {display_name} is."
    return f"I can't work out how long {display_name} will take to get home right now."


@router.get("/api/presence/eta")
async def get_presence_eta(who: str, lang: str = "en") -> Dict[str, Any]:
    """Speak a traffic-aware ETA from a person's location to home (#470).

    The follow-up to the locator: once "where's dad" has answered, the voice
    pipeline offers this. Reuses the same name/role resolution and Find My cache
    as ``/api/presence/locate`` — the origin is the person's cached coordinates,
    the destination is the configured home (``config/location.json``), and the
    duration comes from Google Directions in current traffic. Every failure mode
    (unknown person, already home, no live location, home not set, no key/route)
    degrades to a spoken fallback, never an error — same contract as locate.
    """

    lang = _locate_lang(lang)
    now = now_utc()
    cache = await _cache_for_locate(now=now)
    entity_id, display_name, icloud_entities, people = _resolve_presence_target(who, cache)

    def _reply(speech: str, *, found: bool = True, eta_minutes: Optional[int] = None) -> Dict[str, Any]:
        result = {
            "found": found,
            "entity_id": entity_id,
            "name": display_name if found else who.strip(),
            "eta_minutes": eta_minutes,
            "speech": speech,
        }
        append_activity("presence_eta", {"who": who, "lang": lang, **result})
        return result

    if entity_id is None:
        speech = (
            f"No sé quién es {who.strip()}."
            if lang == "es"
            else f"I don't know who {who.strip()} is."
        )
        return _reply(speech, found=False)

    # Origin coordinates + at-home state, read from the same two sources the
    # locator uses. A Find My entity may carry live coordinates; a webhook person
    # carries only a home/away state (no coordinates, so no routable origin).
    origin: Optional[Tuple[float, float]] = None
    at_home = False
    if entity_id in icloud_entities:
        entity = icloud_entities[entity_id]
        at_home = entity.at_home is True
        if entity.has_location and entity.latitude is not None and entity.longitude is not None:
            origin = (entity.latitude, entity.longitude)
    elif entity_id in people:
        at_home = people[entity_id].state == "home"
    # else: known only via a role/display-name alias, absent from both live
    # sources right now (the diagnostics refresher is down) — no origin, so the
    # "no live location" fallback below is the honest answer.

    if at_home:
        speech = (
            f"{display_name} ya está en casa."
            if lang == "es"
            else f"{display_name} is already home."
        )
        return _reply(speech)

    if origin is None:
        speech = (
            f"No sé exactamente dónde está {display_name}, así que no puedo calcular el trayecto."
            if lang == "es"
            else f"I don't know exactly where {display_name} is, so I can't work out the trip."
        )
        return _reply(speech)

    home = load_location_config()
    if home is None:
        speech = (
            "No tengo configurada la ubicación de casa, así que no puedo calcular el trayecto."
            if lang == "es"
            else "Home location isn't set, so I can't work out the trip."
        )
        return _reply(speech)

    travel = await fetch_travel_time(
        origin_lat=origin[0],
        origin_lon=origin[1],
        dest_lat=home.lat,
        dest_lon=home.lon,
    )
    if not travel.available or travel.duration_s is None:
        return _reply(_eta_unavailable_speech(display_name, travel.reason, lang=lang))

    minutes = max(1, round(travel.duration_s / 60))
    return _reply(_eta_speech(display_name, travel.duration_s, lang=lang), eta_minutes=minutes)


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


async def _reverse_geocode(lat: float, lon: float) -> Dict[str, Any]:
    """Look up (and cache) a short place label for coordinates via Nominatim.

    Shared by the browser-facing ``/api/location/reverse`` endpoint and the
    voice-bridge locate resolution (#442) — same cache, so a coordinate already
    looked up for the Presence card's place label costs nothing to reuse for
    speech.
    """

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


@router.get("/api/location/reverse")
async def reverse_location(lat: float, lon: float) -> Dict[str, Any]:
    """Return a short place label for coordinates using OpenStreetMap Nominatim."""

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    return await _reverse_geocode(lat, lon)
