"""Persisted named-place list for the presence locator (issue #438).

The browser edits a single flat list of custom places (e.g. "Roberto's work",
"the gym"), each with a radius. Modeled on :mod:`src.security_schedules` — the
same "browser edits the whole list, server normalizes and replaces it" shape,
reusing :mod:`src._schedule_store`'s read/save/id helpers.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional

from src._schedule_store import read_json, safe_id, save_json
from src.presence_client import distance_m

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PLACES_PATH = _CONFIG_DIR / "presence_places.json"

DEFAULT_RADIUS_M = 150.0
UNKNOWN_PLACE = "Away — location unknown"


@dataclass(frozen=True)
class PresencePlace:
    """One user-configured named place (label + coordinates + radius)."""

    id: str
    label: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_RADIUS_M


def _clean_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_label(value: Any, fallback: str) -> str:
    label = str(value or "").strip()
    return label or fallback


def clean_place(raw: dict, fallback_id: str) -> PresencePlace:
    """Coerce untrusted JSON/API data into a place entry."""

    lat = max(-90.0, min(90.0, _clean_float(raw.get("lat"), 0.0)))
    lon = max(-180.0, min(180.0, _clean_float(raw.get("lon"), 0.0)))
    radius_m = max(10.0, _clean_float(raw.get("radius_m"), DEFAULT_RADIUS_M))
    return PresencePlace(
        id=safe_id(raw.get("id"), fallback_id),
        label=_clean_label(raw.get("label"), fallback_id),
        lat=lat,
        lon=lon,
        radius_m=radius_m,
    )


def load_presence_places(path: Optional[Path] = None) -> List[PresencePlace]:
    """Return the persisted named-place list, or ``[]`` if absent."""

    target = Path(path) if path is not None else PLACES_PATH
    raw = read_json(target, [])
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_place(item, f"place-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def save_presence_places(places: List[PresencePlace], path: Optional[Path] = None) -> None:
    """Atomically persist the whole named-place list."""

    target = Path(path) if path is not None else PLACES_PATH
    save_json(target, [asdict(place) for place in places])


def set_presence_places(
    raw_places: List[dict], path: Optional[Path] = None
) -> List[PresencePlace]:
    """Replace the named-place list with normalized entries and return it."""

    places = [
        clean_place(item, f"place-{idx}")
        for idx, item in enumerate(raw_places, start=1)
        if isinstance(item, dict)
    ]
    save_presence_places(places, path)
    return places


def resolve_place(
    *,
    latitude: Optional[float],
    longitude: Optional[float],
    at_home: Optional[bool],
    has_location: bool,
    places: List[PresencePlace],
) -> str:
    """Resolve a person/entity's current place label from cached coordinates.

    The closest configured place whose radius contains the point wins; falls
    back to "Home" (``at_home``), then "Away" when located but unmatched, else
    :data:`UNKNOWN_PLACE` when there is no usable location at all (stale or
    never located — e.g. a webhook-only person with no Find My coordinates).
    """

    if latitude is not None and longitude is not None:
        candidates = [
            (distance_m(latitude, longitude, place.lat, place.lon), place)
            for place in places
        ]
        within_radius = [
            (dist, place) for dist, place in candidates if dist <= place.radius_m
        ]
        if within_radius:
            _, nearest = min(within_radius, key=lambda pair: pair[0])
            return nearest.label

    if at_home:
        return "Home"
    if has_location:
        return "Away"
    return UNKNOWN_PLACE
