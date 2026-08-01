"""Expected-generation forecast for the Energy tab (issue #39).

Turns Open-Meteo's hourly **global tilted irradiance** (GTI, W/m²) into a rough
expected-generation curve for the home's PV array, for one of three days —
yesterday, today, or tomorrow. This is the read/visualisation half of the
eventual solar load-balancing goal: a forecast to compare against the measured
generation the metered side records, *not* a control input.

Source & model (deliberately self-contained, approximate):

* One keyless Open-Meteo call **per sub-array** (the same host the weather tile
  already uses), asking for ``global_tilted_irradiance`` at that sub-array's
  tilt + azimuth across ``past_days=1`` … ``forecast_days=2`` so all three days
  come back in a single request per sub-array. (:func:`fetch_pv_forecast_for_date`
  widens ``past_days`` for the read-only sun-position diagnostic, issue #590 —
  the three named days always resolve to ``past_days=1``.) Open-Meteo's tilt/azimuth params
  don't support batching multiple orientations in one call (issue #555), so a
  multi-orientation array fires one request per sub-array, concurrently, over a
  shared session.
* Per hour, per sub-array, ``expected_W = kwp · GTI/1000 · performance_ratio``
  (kWp is defined at the 1000 W/m² STC reference, so GTI/1000 is the fraction of
  peak); GTI is a preceding-hour mean, so one hour of it integrates straight to
  ``expected_Wh``. The sub-array totals are summed per hour into the combined
  curve.

Array parameters come from ``config/pv_system.json`` (:mod:`src.pv_system_config`)
and the coordinates from ``config/location.json`` (:mod:`src.location_config`,
shared with the weather tile). Either missing → ``available=False`` with a
``reason``; an Open-Meteo failure on any sub-array is quiet too (HTTP-200-
friendly), never a 500 — the whole forecast is unavailable rather than silently
under-counting a missing orientation.

UI-free: imported by the energy API, never imports the UI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

from src.location_config import LocationConfig, load_location_config
from src.pv_system_config import PvArray, PvSystemConfig, load_pv_system_config

logger = logging.getLogger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 8.0

# Day selector → offset from today's local date.
_DAY_OFFSETS = {"yesterday": -1, "today": 0, "tomorrow": 1}

# How far back Open-Meteo's forecast endpoint will serve ``past_days``. Beyond
# this the request is refused upstream, so a deeper day is reported unavailable
# here rather than turned into a network error the caller has to interpret.
MAX_PAST_DAYS = 92


@dataclass
class PvForecast:
    """An expected-generation curve for one day (or an "unavailable" marker)."""

    available: bool
    day: str
    expected: List[Dict[str, Any]] = field(default_factory=list)
    expected_total_wh: float = 0.0
    reason: Optional[str] = None
    # The array parameters the curve was computed from (for the UI to display),
    # populated only on an available forecast.
    system: Optional[Dict[str, Any]] = None


def _unavailable(day: str, reason: str) -> PvForecast:
    return PvForecast(available=False, day=day, reason=reason)


async def _fetch_array_gti(
    session: aiohttp.ClientSession,
    location: LocationConfig,
    array: PvArray,
    past_days: int = 1,
) -> Dict[str, Any]:
    """One Open-Meteo hourly-GTI request for a single sub-array's orientation.

    ``past_days`` is widened only when a caller asks for a day older than
    yesterday (the read-only sun-position diagnostic, issue #590). The three
    named days all resolve to ``past_days=1``, so the forecast card's request —
    and therefore its answer — is byte-for-byte what it was before that
    diagnostic existed.
    """
    params = {
        "latitude": location.lat,
        "longitude": location.lon,
        "hourly": "global_tilted_irradiance",
        "tilt": array.tilt_deg,
        "azimuth": array.azimuth_deg,
        "past_days": past_days,
        "forecast_days": 2,
        "timezone": "auto",
    }
    async with session.get(_OPEN_METEO_URL, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_pv_forecast(
    day: str = "today",
    *,
    system: Optional[PvSystemConfig] = None,
    location: Optional[LocationConfig] = None,
    today: Optional[date] = None,
) -> PvForecast:
    """Hourly expected-generation curve (Wh) for ``day`` ∈ yesterday/today/tomorrow.

    ``system`` / ``location`` / ``today`` are injectable for tests; in normal use
    they are read from config and the local clock. Returns an ``available=False``
    forecast (never raises) when the array/location is unconfigured or Open-Meteo
    cannot be reached for any sub-array.
    """
    if day not in _DAY_OFFSETS:
        raise ValueError(f"unknown day: {day!r}")

    base = today or datetime.now().date()
    return await fetch_pv_forecast_for_date(
        base + timedelta(days=_DAY_OFFSETS[day]),
        label=day,
        system=system,
        location=location,
        today=base,
    )


async def fetch_pv_forecast_for_date(
    target_day: date,
    *,
    label: Optional[str] = None,
    system: Optional[PvSystemConfig] = None,
    location: Optional[LocationConfig] = None,
    today: Optional[date] = None,
) -> PvForecast:
    """The same curve as :func:`fetch_pv_forecast`, for an arbitrary date.

    The named-day API above is the forecast card's entry point and stays the
    canonical one; this is what the read-only sun-position diagnostic (issue
    #590) calls to reach a day further back than "yesterday". The model is
    identical — only how far back the irradiance request reaches differs.

    ``label`` is what the result reports as its ``day`` (the ISO date by
    default), so the named-day caller keeps reporting "yesterday".
    """
    day = label or target_day.isoformat()

    system = system or load_pv_system_config()
    if system is None:
        return _unavailable(day, "not_configured")

    location = location or load_location_config()
    if location is None:
        return _unavailable(day, "no_location")

    # One past day is always requested (the forecast card's yesterday tab); a
    # deeper target widens the window and nothing else.
    delta_days = (target_day - (today or datetime.now().date())).days
    past_days = max(1, -delta_days)
    if past_days > MAX_PAST_DAYS:
        return _unavailable(day, "too_old")

    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            responses = await asyncio.gather(
                *(
                    _fetch_array_gti(session, location, array, past_days)
                    for array in system.arrays
                )
            )
    except Exception as exc:  # noqa: BLE001 — forecast is decorative, fail quiet
        logger.warning("⚠️ Failed to read PV forecast: %s", exc)
        return _unavailable(day, "unreachable")

    iso_prefix = target_day.isoformat()
    totals_by_hour: Dict[int, float] = {}

    for array, data in zip(system.arrays, responses):
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        gti = hourly.get("global_tilted_irradiance") or []
        if not times or len(times) != len(gti):
            return _unavailable(day, "no_data")

        # kWp is defined at 1000 W/m² STC: expected_W = kwp · GTI/1000 · PR. GTI
        # is a preceding-hour mean, so one hour of it is expected_Wh directly.
        scale = array.kwp * system.performance_ratio  # × (GTI/1000) × 1000h→Wh ⇒ × GTI

        for stamp, irradiance in zip(times, gti):
            if not str(stamp).startswith(iso_prefix):
                continue
            watts = float(irradiance) if irradiance is not None else 0.0
            wh = max(0.0, scale * watts)
            try:
                hour = datetime.fromisoformat(str(stamp)).hour
            except ValueError:
                continue
            totals_by_hour[hour] = totals_by_hour.get(hour, 0.0) + wh

    if not totals_by_hour:
        return _unavailable(day, "no_data")

    expected = [
        {"hour": hour, "wh": round(wh, 1)} for hour, wh in sorted(totals_by_hour.items())
    ]
    total_wh = sum(totals_by_hour.values())

    return PvForecast(
        available=True,
        day=day,
        expected=expected,
        expected_total_wh=round(total_wh, 1),
        system={
            "arrays": [
                {"kwp": a.kwp, "tilt_deg": a.tilt_deg, "azimuth_deg": a.azimuth_deg}
                for a in system.arrays
            ],
            "total_kwp": round(system.total_kwp, 3),
            "performance_ratio": system.performance_ratio,
        },
    )
