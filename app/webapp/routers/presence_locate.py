"""Locate + traffic-aware ETA voice bridge over the presence diagnostics cache.

Split out of ``routers/presence.py`` (issue #702) — the two voice-bridge
endpoints (``…/locate`` #438, ``…/eta`` #470) share all their resolution and
staleness-handling machinery and touch none of the webhook/CRUD state the
rest of the presence router owns. Both read the background diagnostics
refresher's cache, same as ``GET /api/presence``, but refresh on demand when
that cache is stale (#442); ``lang=es`` makes the ready-made ``speech``
Spanish for the "Hey Mycroft" pipeline (#446), resolution itself is
language-agnostic. Every failure mode (unknown person, already home, no live
location, home not set, no key/route) degrades to a spoken fallback, never an
error.

Reverse-geocoding an unmatched "Away" location (#442) reuses
``routers.presence._reverse_geocode`` — the same Nominatim cache the
``/api/location/reverse`` endpoint serves, so a coordinate already looked up
for the Presence card's place label costs nothing to reuse for speech. Kept
as a call through the sibling module (not a plain ``from``-import) so tests
that monkeypatch ``app.webapp.routers.presence._reverse_geocode`` still take
effect here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter

from app.webapp._env import _env_int
from app.webapp.presence_refresher import PresenceDiagnosticsCache, get_cache, refresh_once
from app.webapp.routers import presence as _presence_router
from src.activity_log import append_activity
from src.location_config import load_location_config
from src.presence_client import PresenceEntity
from src.presence_display_names import load_presence_display_names
from src.presence_engine import load_people, now_utc
from src.presence_places import UNKNOWN_PLACE, load_presence_places, resolve_place
from src.presence_roles import load_presence_roles, resolve_person
from src.travel_time import fetch_travel_time

logger = logging.getLogger(__name__)

router = APIRouter()

_LOCATE_STALE_AFTER_S_DEFAULT = 120
_LOCATE_REFRESH_TIMEOUT_S_DEFAULT = 12


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

    The timeout default was raised 5s -> 12s (#491): a real Find My locate,
    especially one that has to wake a device over the network for a live fix,
    routinely takes longer than 5 seconds, so the on-demand refresh was losing
    its race almost every time and silently falling back to the stale
    background-cadence cache. ``refresh_once()`` also fetches every configured
    account concurrently rather than splitting this budget across them
    serially.
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


def _place_speech(display_name: str, place: str, *, lang: str = "en", recency: Optional[str] = None) -> str:
    if lang == "es":
        if place == "Home":
            base = f"{display_name} está en casa"
        elif place in ("Away", UNKNOWN_PLACE):
            base = f"{display_name} está fuera — no sé exactamente dónde"
        else:
            base = f"{display_name} está en {place}"
        return f"{base}, visto por última vez {recency}." if recency else f"{base}."
    if place == "Home":
        base = f"{display_name} is home"
    elif place in ("Away", UNKNOWN_PLACE):
        base = f"{display_name} is away — I don't know exactly where"
    else:
        base = f"{display_name} is at {place}"
    return f"{base}, last seen {recency}." if recency else f"{base}."


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
    result = await _presence_router._reverse_geocode(entity.latitude, entity.longitude)
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
            "last_seen": None,
            "speech": not_found_speech,
        }
        append_activity("presence_locate", {"who": who, "lang": lang, **result})
        return result

    last_seen: Optional[datetime] = None
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
        last_seen = entity.last_seen
        recency = _recency_phrase((now - last_seen).total_seconds(), lang=lang) if last_seen else None
        speech = _place_speech(display_name, place, lang=lang, recency=recency)
    elif entity_id in people:
        person = people[entity_id]
        place = resolve_place(
            latitude=None,
            longitude=None,
            at_home=person.state == "home",
            has_location=False,
            places=places,
        )
        last_seen = person.updated_at
        recency = _recency_phrase((now - last_seen).total_seconds(), lang=lang)
        speech = _place_speech(display_name, place, lang=lang, recency=recency)
    else:
        # Known via a role/display-name alias but absent from both live sources
        # right now — the Find My cache is empty because the diagnostics
        # refresher is down, not because this person is simply "away" (#442).
        place = UNKNOWN_PLACE
        speech = _broken_source_speech(display_name, cache, lang=lang)

    result = {
        "found": True,
        "entity_id": entity_id,
        "name": display_name,
        "place": place,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "speech": speech,
    }
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


def _recency_phrase(age_s: float, *, lang: str) -> str:
    """Relative "last seen" recency for the locator's speech + JSON payload
    (#492) — reuses ``_duration_phrase``'s hour/minute conventions rather than
    inventing a new scale. Under a minute reads as "just now" instead of
    rounding up to a misleadingly precise "1 minute ago"."""

    if age_s < 60:
        return "justo ahora" if lang == "es" else "just now"
    minutes = max(1, round(age_s / 60))
    phrase = _duration_phrase(minutes, lang=lang)
    return f"hace {phrase}" if lang == "es" else f"{phrase} ago"


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
