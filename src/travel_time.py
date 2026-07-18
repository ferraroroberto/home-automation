"""Traffic-aware travel time to home via the Google Directions API (issue #470).

The UI-free core behind the voice "how long to get home?" follow-up: given a
person's current coordinates and the home coordinates, ask Google Directions for
the driving duration **in current traffic** and hand back a small result the
caller turns into spoken text.

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

DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"


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
    duration_text: Optional[str] = None
    reason: str = ""
    detail: str = ""


def _api_key() -> str:
    """The Google Maps key from ``.env`` (empty string when unset)."""

    load_dotenv(override=True)
    return (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()


def _parse_directions(data: Any) -> TravelTime:
    """Map a raw Directions JSON body to a ``TravelTime`` (pure, no I/O).

    Prefers ``duration_in_traffic`` (present because we send ``departure_time``)
    and falls back to the free-flow ``duration``. ``ZERO_RESULTS`` is a real "no
    drivable route" answer; every other non-``OK`` status (REQUEST_DENIED,
    OVER_QUERY_LIMIT, INVALID_REQUEST, …) is a configuration/quota error.
    """

    if not isinstance(data, dict):
        return TravelTime(available=False, reason="error", detail="non-object response")

    status = str(data.get("status") or "")
    if status != "OK":
        reason = "no_route" if status == "ZERO_RESULTS" else "error"
        detail = str(data.get("error_message") or status)
        if reason == "error":
            logger.warning("⚠️ Directions returned %s: %s", status, detail)
        return TravelTime(available=False, reason=reason, detail=detail)

    try:
        leg = data["routes"][0]["legs"][0]
        duration = leg.get("duration_in_traffic") or leg["duration"]
        return TravelTime(
            available=True,
            duration_s=int(duration["value"]),
            duration_text=str(duration.get("text") or ""),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("⚠️ Unexpected Directions payload shape: %s", exc)
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

    ``departure_time=now`` is what unlocks ``duration_in_traffic`` in the
    Directions response; without it Google returns only the free-flow duration.
    Any failure (no key, network error, non-``OK`` status) degrades to an
    ``available=False`` result rather than raising.
    """

    key = _api_key()
    if not key:
        return TravelTime(available=False, reason="no_api_key")

    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "mode": "driving",
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": key,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DIRECTIONS_URL, params=params, timeout=timeout_s) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Directions HTTP {resp.status}")
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unavailable"
        logger.warning("⚠️ Travel-time lookup failed: %s", exc)
        return TravelTime(available=False, reason="error", detail=str(exc))

    return _parse_directions(data)
