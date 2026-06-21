"""Expected-generation forecast for the Energy tab (issue #39).

Turns Open-Meteo's hourly **global tilted irradiance** (GTI, W/m²) into a rough
expected-generation curve for the home's PV array, for one of three days —
yesterday, today, or tomorrow. This is the read/visualisation half of the
eventual solar load-balancing goal: a forecast to compare against the measured
generation the SMA side records, *not* a control input.

Source & model (deliberately self-contained, approximate):

* One keyless Open-Meteo call (the same host the weather tile already uses),
  asking for ``global_tilted_irradiance`` at the array's tilt + azimuth across
  ``past_days=1`` … ``forecast_days=2`` so all three days come back in a single
  request.
* Per hour, ``expected_W = kwp · GTI/1000 · performance_ratio`` (kWp is defined
  at the 1000 W/m² STC reference, so GTI/1000 is the fraction of peak); GTI is a
  preceding-hour mean, so one hour of it integrates straight to ``expected_Wh``.

Array parameters come from ``config/pv_system.json`` (:mod:`src.pv_system_config`)
and the coordinates from ``config/location.json`` (:mod:`src.location_config`,
shared with the weather tile). Either missing → ``available=False`` with a
``reason``; an Open-Meteo failure is quiet too (HTTP-200-friendly), never a 500.

UI-free: imported by the energy API, never imports the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

from src.location_config import LocationConfig, load_location_config
from src.pv_system_config import PvSystemConfig, load_pv_system_config

logger = logging.getLogger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 8.0

# Day selector → offset from today's local date.
_DAY_OFFSETS = {"yesterday": -1, "today": 0, "tomorrow": 1}


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
    cannot be reached.
    """
    if day not in _DAY_OFFSETS:
        raise ValueError(f"unknown day: {day!r}")

    system = system or load_pv_system_config()
    if system is None:
        return _unavailable(day, "not_configured")

    location = location or load_location_config()
    if location is None:
        return _unavailable(day, "no_location")

    target_day = (today or datetime.now().date()) + timedelta(days=_DAY_OFFSETS[day])

    params = {
        "latitude": location.lat,
        "longitude": location.lon,
        "hourly": "global_tilted_irradiance",
        "tilt": system.tilt_deg,
        "azimuth": system.azimuth_deg,
        "past_days": 1,
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_OPEN_METEO_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001 — forecast is decorative, fail quiet
        logger.warning("⚠️ Failed to read PV forecast: %s", exc)
        return _unavailable(day, "unreachable")

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    gti = hourly.get("global_tilted_irradiance") or []
    if not times or len(times) != len(gti):
        return _unavailable(day, "no_data")

    # kWp is defined at 1000 W/m² STC: expected_W = kwp · GTI/1000 · PR. GTI is a
    # preceding-hour mean, so one hour of it is expected_Wh directly.
    scale = system.kwp * system.performance_ratio  # × (GTI/1000) × 1000h→Wh ⇒ × GTI
    iso_prefix = target_day.isoformat()

    expected: List[Dict[str, Any]] = []
    total_wh = 0.0
    for stamp, irradiance in zip(times, gti):
        if not str(stamp).startswith(iso_prefix):
            continue
        watts = float(irradiance) if irradiance is not None else 0.0
        wh = max(0.0, scale * watts)
        try:
            hour = datetime.fromisoformat(str(stamp)).hour
        except ValueError:
            continue
        expected.append({"hour": hour, "wh": round(wh, 1)})
        total_wh += wh

    if not expected:
        return _unavailable(day, "no_data")

    expected.sort(key=lambda p: p["hour"])
    return PvForecast(
        available=True,
        day=day,
        expected=expected,
        expected_total_wh=round(total_wh, 1),
        system={
            "kwp": system.kwp,
            "tilt_deg": system.tilt_deg,
            "azimuth_deg": system.azimuth_deg,
            "performance_ratio": system.performance_ratio,
        },
    )
