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
* **Optionally** (issue #591, off by default) a PVWatts-style panel-temperature
  derate multiplies that: hot cells lose efficiency, a loss that swings from ~0%
  at 25 °C cell to ~13% at 62 °C and which a constant derate cannot track. The
  term is armed by ``thermal_model_enabled`` in ``config/pv_system.json``; with
  it off, not even the upstream request changes, so the card's numbers are
  exactly what they were. Turning it on also requires migrating
  ``performance_ratio`` to a system-loss-only factor — see
  :func:`src.pv_system_config.thermal_migration_error`, which makes the
  half-migrated combination refuse to compute rather than under-report ~10%.

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
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from src.location_config import LocationConfig, load_location_config
from src.pv_system_config import (
    PvArray,
    PvSystemConfig,
    load_pv_system_config,
    thermal_migration_error,
)

logger = logging.getLogger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 8.0

# How long one upstream response is reused, keyed by (location, array-set, day)
# — see :func:`_cache_key`. Open-Meteo's hourly irradiance only changes on an
# hourly grid, so a request per card render (issue #597) is pure waste; every
# render inside this window is served from the cache instead.
_CACHE_TTL_S = 900

# How stale a cached forecast may be and still be served while backing off a
# 429, rather than blanking the card. Generous relative to the TTL above: the
# point isn't freshness, it's riding out a sustained rate-limit window without
# showing "unavailable" for a curve that was fine minutes ago.
_STALE_SERVE_MAX_S = 6 * 3600

# How long to stay quiet after a 429, doubling per consecutive one — same
# shape as huawei_client's login backoff, for the same reason: hammering an
# endpoint that just told you to slow down is how a transient rate limit turns
# into a sustained one.
_FAILURE_BACKOFF_BASE_S = 60
_FAILURE_BACKOFF_MAX_S = 900

# ------------------------------------------- panel temperature (issue #591)
# A PVWatts-style NOCT cell-temperature model. #578's diagnosis fitted these two
# numbers against this array's measured output and landed within ±5% for every
# morning and midday hour, so they start as constants rather than as config the
# user has no way to calibrate. Wind speed is deliberately NOT modelled: the
# still-air NOCT form already fits to within the measurement noise here, and the
# residual afternoon gap it does not close is geometric (horizon shading, #578
# part b), not thermal — a wind term would only appear to absorb it.

# Power temperature coefficient, per °C away from the 25 °C STC cell reference.
THERMAL_GAMMA_PER_C = -0.0035
# Nominal Operating Cell Temperature: the cell temperature reached at 800 W/m²
# in 20 °C still air, the two reference conditions below.
THERMAL_NOCT_C = 45.0
_NOCT_IRRADIANCE_W = 800.0
_NOCT_AMBIENT_C = 20.0
_STC_CELL_C = 25.0

# Day selector → offset from today's local date.
_DAY_OFFSETS = {"yesterday": -1, "today": 0, "tomorrow": 1}

# How far back Open-Meteo's forecast endpoint will serve ``past_days``. Beyond
# this the request is refused upstream, so a deeper day is reported unavailable
# here rather than turned into a network error the caller has to interpret.
MAX_PAST_DAYS = 92

# One aiohttp session reused across calls (issue #597) — opening a fresh one
# per render, per sub-array, was the other half of the request storm. The lock
# only guards creation; the session itself is safe for concurrent requests.
_session: Optional[aiohttp.ClientSession] = None
_session_lock: Optional[asyncio.Lock] = None

# cache_key -> (monotonic timestamp, forecast for that exact target_day). The
# ``day``/``label`` a caller asked for is applied on top of the cached value —
# see :func:`_relabel` — so "today" and an equivalent ``fetch_pv_forecast_for_date``
# call share one cache entry, the way huawei_client shares one FusionSolar read.
_forecast_cache: Dict[tuple, Tuple[float, "PvForecast"]] = {}

# Monotonic deadline before which no upstream call is attempted after a 429,
# and the streak that set it — see :func:`_note_failure` / :func:`_note_success`.
_failure_until: float = 0.0
_failure_streak: int = 0


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


def _relabel(cached: PvForecast, day: str) -> PvForecast:
    """A cache hit for the same ``target_day`` under whichever ``day`` label the
    caller asked for — "today" and its equivalent ISO date share one entry."""
    if cached.day == day:
        return cached
    return replace(cached, day=day)


def _cache_key(
    target_day: date, system: PvSystemConfig, location: LocationConfig
) -> tuple:
    """Identifies the exact upstream request this forecast would make.

    Deliberately excludes ``today``/``past_days``: for a fixed ``target_day``,
    Open-Meteo answers the same regardless of how far the request's lookback
    window reaches, so the read-only sun-position diagnostic (#590) and the
    named-day card share a cache entry for the same date.
    """
    arrays = tuple((a.kwp, a.tilt_deg, a.azimuth_deg) for a in system.arrays)
    return (
        round(location.lat, 4),
        round(location.lon, 4),
        arrays,
        system.performance_ratio,
        system.thermal_model_enabled,
        target_day.isoformat(),
    )


def _prune_cache(now: float) -> None:
    """Drop cache entries too old to ever be served again.

    ``target_day`` rolls forward daily and the sun-position diagnostic can ask
    for any of the last 92 days, so without this the dict would grow by a new
    entry every day for as long as the tray stays up. Anything past the
    stale-serve horizon is dead weight — it will never be read again either
    as a TTL hit or as a 429 fallback.
    """
    stale = [k for k, (ts, _f) in _forecast_cache.items() if now - ts >= _STALE_SERVE_MAX_S]
    for k in stale:
        del _forecast_cache[k]


def _backoff_for(streak: int) -> float:
    """Seconds to stay quiet after ``streak`` consecutive 429s."""
    if streak < 1:
        return 0.0
    return min(_FAILURE_BACKOFF_MAX_S, _FAILURE_BACKOFF_BASE_S * 2 ** (streak - 1))


def _note_failure(now: float) -> None:
    """Open (or lengthen) the backoff window after a 429."""
    global _failure_until, _failure_streak

    _failure_streak += 1
    wait = _backoff_for(_failure_streak)
    _failure_until = now + wait
    logger.warning(
        "⚠️ PV forecast rate-limited by Open-Meteo (%d consecutive 429(s)); "
        "not retrying for %ds",
        _failure_streak,
        wait,
    )


def _note_success() -> None:
    """Clear the backoff after a good read."""
    global _failure_until, _failure_streak

    if _failure_streak:
        logger.info("✅ PV forecast recovered after %d rate-limited attempt(s)", _failure_streak)
    _failure_streak = 0
    _failure_until = 0.0


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def _get_session() -> aiohttp.ClientSession:
    """Return the module's shared session, opening (or reopening) it as needed.

    One session reused across calls (issue #597) rather than a fresh
    connection pool per render — the same fix ``huawei_client`` already makes
    for FusionSolar. Guarded by a lock only around creation; concurrent
    requests against an already-open session are fine.
    """
    global _session

    async with _get_session_lock():
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S)
            )
        return _session


def cell_temperature_c(air_c: float, gti_w: float) -> float:
    """NOCT cell temperature (°C) for an hour of ``gti_w`` at ``air_c`` ambient."""
    rise = (THERMAL_NOCT_C - _NOCT_AMBIENT_C) / _NOCT_IRRADIANCE_W
    return air_c + rise * gti_w


def thermal_derate(air_c: float, gti_w: float) -> float:
    """Efficiency multiplier (≤1 when hot) for one hour's irradiance + ambient.

    Floored at zero: γ is only linear over the range panels actually reach, and
    a negative factor would turn a hot hour into negative generation.
    """
    cell = cell_temperature_c(air_c, gti_w)
    return max(0.0, 1.0 + THERMAL_GAMMA_PER_C * (cell - _STC_CELL_C))


async def _fetch_array_gti(
    session: aiohttp.ClientSession,
    location: LocationConfig,
    array: PvArray,
    past_days: int = 1,
    with_temperature: bool = False,
) -> Dict[str, Any]:
    """One Open-Meteo hourly-GTI request for a single sub-array's orientation.

    ``past_days`` is widened only when a caller asks for a day older than
    yesterday (the read-only sun-position diagnostic, issue #590). The three
    named days all resolve to ``past_days=1``, so the forecast card's request —
    and therefore its answer — is byte-for-byte what it was before that
    diagnostic existed.

    ``with_temperature`` adds ``temperature_2m`` to the *same* request (issue
    #591) — no second call, and no change at all to the request when the thermal
    term is off, so the disabled path cannot drift on the back of a feature that
    is not running.
    """
    hourly = "global_tilted_irradiance"
    if with_temperature:
        hourly += ",temperature_2m"
    params = {
        "latitude": location.lat,
        "longitude": location.lon,
        "hourly": hourly,
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

    # Refuse the half-migrated combination rather than quietly subtracting the
    # thermal loss twice (issue #591) — a wrong curve is worse than no curve.
    mismatch = thermal_migration_error(system)
    if mismatch is not None:
        logger.warning("⚠️ PV forecast refused: %s", mismatch)
        return _unavailable(day, "thermal_ratio_unmigrated")

    location = location or load_location_config()
    if location is None:
        return _unavailable(day, "no_location")

    # One past day is always requested (the forecast card's yesterday tab); a
    # deeper target widens the window and nothing else.
    delta_days = (target_day - (today or datetime.now().date())).days
    past_days = max(1, -delta_days)
    if past_days > MAX_PAST_DAYS:
        return _unavailable(day, "too_old")

    key = _cache_key(target_day, system, location)
    now = time.monotonic()

    cached = _forecast_cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_S:
        return _relabel(cached[1], day)

    if now < _failure_until:
        # Backing off a 429 — serve the last good curve rather than a blank
        # card, as long as it isn't too old to still describe today's weather.
        if cached is not None and now - cached[0] < _STALE_SERVE_MAX_S:
            logger.info(
                "ℹ️ PV forecast serving cached curve (%.0fs old) while "
                "rate-limit backoff is active",
                now - cached[0],
            )
            return _relabel(cached[1], day)
        return _unavailable(day, "rate_limited")

    try:
        session = await _get_session()
        responses = await asyncio.gather(
            *(
                _fetch_array_gti(
                    session,
                    location,
                    array,
                    past_days,
                    with_temperature=system.thermal_model_enabled,
                )
                for array in system.arrays
            )
        )
    except aiohttp.ClientResponseError as exc:
        if exc.status == 429:
            _note_failure(now)
            if cached is not None and now - cached[0] < _STALE_SERVE_MAX_S:
                logger.info(
                    "ℹ️ PV forecast serving cached curve (%.0fs old) after a 429",
                    now - cached[0],
                )
                return _relabel(cached[1], day)
            return _unavailable(day, "rate_limited")
        logger.warning("⚠️ Failed to read PV forecast: %s", exc)
        return _unavailable(day, "unreachable")
    except Exception as exc:  # noqa: BLE001 — forecast is decorative, fail quiet
        logger.warning("⚠️ Failed to read PV forecast: %s", exc)
        return _unavailable(day, "unreachable")

    _note_success()

    iso_prefix = target_day.isoformat()
    totals_by_hour: Dict[int, float] = {}

    for array, data in zip(system.arrays, responses):
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        gti = hourly.get("global_tilted_irradiance") or []
        if not times or len(times) != len(gti):
            return _unavailable(day, "no_data")

        if system.thermal_model_enabled:
            air = hourly.get("temperature_2m") or []
            if len(air) != len(times):
                return _unavailable(day, "no_data")
        else:
            air = [None] * len(times)

        # kWp is defined at 1000 W/m² STC: expected_W = kwp · GTI/1000 · PR. GTI
        # is a preceding-hour mean, so one hour of it is expected_Wh directly.
        scale = array.kwp * system.performance_ratio  # × (GTI/1000) × 1000h→Wh ⇒ × GTI

        for stamp, irradiance, air_c in zip(times, gti, air):
            if not str(stamp).startswith(iso_prefix):
                continue
            watts = float(irradiance) if irradiance is not None else 0.0
            wh = max(0.0, scale * watts)
            # A null ambient for one hour (an upstream gap) falls back to no
            # derate — the pre-#591 value — rather than dropping the hour from
            # a curve whose irradiance is perfectly good.
            if system.thermal_model_enabled and air_c is not None:
                wh *= thermal_derate(float(air_c), watts)
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

    described: Dict[str, Any] = {
        "arrays": [
            {"kwp": a.kwp, "tilt_deg": a.tilt_deg, "azimuth_deg": a.azimuth_deg}
            for a in system.arrays
        ],
        "total_kwp": round(system.total_kwp, 3),
        "performance_ratio": system.performance_ratio,
    }
    # Only present when the term is armed: with it off the payload — keys
    # included, not just values — is exactly what it was before #591.
    if system.thermal_model_enabled:
        described["thermal_model"] = {
            "gamma_per_c": THERMAL_GAMMA_PER_C,
            "noct_c": THERMAL_NOCT_C,
        }

    result = PvForecast(
        available=True,
        day=day,
        expected=expected,
        expected_total_wh=round(total_wh, 1),
        system=described,
    )
    _prune_cache(now)
    _forecast_cache[key] = (now, result)
    return result
