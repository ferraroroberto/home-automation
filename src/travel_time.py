"""Traffic-aware travel time to home via the Google Routes API (issue #470/#472).

The UI-free core behind the voice "how long to get home?" follow-up: given a
person's current coordinates and the home coordinates, ask Google for the
driving duration **in current traffic** and hand back a small result the caller
turns into spoken text.

Uses the modern **Routes API** (``directions/v2:computeRoutes``); the legacy
Directions API this originally called is deprecated and no longer enabled for
newer Cloud projects (#472). ``routingPreference: TRAFFIC_AWARE`` gives the
in-traffic duration and defaults the departure to now, so no timestamp handling
is needed.

Mirrors the presence locator's graceful-degradation contract: this never raises
for a missing API key, an unreachable API, or no drivable route — it returns a
``TravelTime`` with ``available=False`` and a machine ``reason`` the router maps
to a sensible spoken fallback, exactly as ``_broken_source_speech`` does for a
down Find My source. That keeps the failure modes speakable rather than 500s.

``GOOGLE_MAPS_API_KEY`` is read from ``.env`` (the repo is public — the key is
env-only, never committed). No key configured is a first-class "unavailable"
result, not an error, so the feature is inert until the key is provisioned.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger("melcloud.travel_time")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


@dataclass
class TravelTime:
    """A driving-duration lookup result.

    ``available`` gates everything else: when ``True``, ``duration_s`` is the
    trip length in seconds (in current traffic when Google returns it, plain
    duration otherwise) and ``duration_text`` is Google's own human label (kept
    for the activity-log breadcrumb — the spoken minutes are formatted by the
    caller so the app owns the speech). When ``False``, ``reason`` is one of
    ``no_api_key`` / ``no_route`` / ``error`` and ``detail`` carries context.
    """

    available: bool
    duration_s: Optional[int] = None
    duration_text: Optional[str] = None  # raw API duration (e.g. "1080s"), for the log breadcrumb
    reason: str = ""
    detail: str = ""


def _api_key() -> str:
    """The Google Maps key from ``.env`` (empty string when unset)."""

    load_dotenv(override=True)
    return (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()


def _parse_routes(data: Any) -> TravelTime:
    """Map a Routes API ``computeRoutes`` 200 body to a ``TravelTime`` (pure, no I/O).

    A successful response is ``{"routes": [{"duration": "1080s"}]}`` — the
    duration is a seconds string with an ``s`` suffix. An empty ``routes`` list
    is a real "no drivable route" answer. API-level failures (bad key, API not
    enabled, quota) arrive as 4xx HTTP statuses and are handled in
    :func:`fetch_travel_time`, not here.
    """

    if not isinstance(data, dict):
        return TravelTime(available=False, reason="error", detail="non-object response")

    routes = data.get("routes") or []
    if not routes:
        return TravelTime(available=False, reason="no_route")

    try:
        raw = routes[0]["duration"]  # e.g. "1080s"
        seconds = int(float(str(raw).rstrip("s")))
        return TravelTime(available=True, duration_s=seconds, duration_text=str(raw))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("⚠️ Unexpected Routes payload shape: %s", exc)
        return TravelTime(available=False, reason="error", detail=f"payload: {exc}")


async def fetch_travel_time(
    *,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    timeout_s: int = 10,
) -> TravelTime:
    """Return the driving duration from origin to destination, in current traffic.

    ``routingPreference: TRAFFIC_AWARE`` gives the in-traffic duration and
    defaults the departure to now. Any failure (no key, network error, 4xx from
    the API) degrades to an ``available=False`` result rather than raising.
    """

    key = _api_key()
    if not key:
        return TravelTime(available=False, reason="no_api_key")

    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lon}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lon}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        # Field mask is mandatory on the Routes API; ask only for what we speak.
        "X-Goog-FieldMask": "routes.duration",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ROUTES_URL, json=body, headers=headers, timeout=timeout_s
            ) as resp:
                status = resp.status
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unavailable"
        logger.warning("⚠️ Travel-time lookup failed: %s", exc)
        return TravelTime(available=False, reason="error", detail=str(exc))

    if status >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str((data.get("error") or {}).get("message") or status)
        logger.warning("⚠️ Routes API HTTP %s: %s", status, detail)
        return TravelTime(available=False, reason="error", detail=detail)

    return _parse_routes(data)
