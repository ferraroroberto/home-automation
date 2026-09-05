"""Live energy-flow API over the Huawei FusionSolar cloud.

``GET /api/energy`` returns the home's instantaneous energy flow — grid
import/export measured by the inverter's power sensor, PV production, and the
house consumption + derived PV surplus. This is the read side of the eventual
solar load-balancing automation.

Partial data is normal and returned with 200: the inverter sleeps at night, so
``pv_power_w`` is ``null`` and ``inverter_reachable`` is ``false`` then.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.energy_history import (
    MIN_TRUSTED_COVERAGE,
    aggregate,
    framed_buckets,
    hourly_day,
    hourly_range,
    recent_samples,
)
from src._schedule_store import StoreUnreadableError
from src.location_config import load_location_config
from src.pv_forecast import MAX_PAST_DAYS, fetch_pv_forecast, fetch_pv_forecast_for_date
from src.pv_system_config import (
    PvArray,
    PvHorizonPoint,
    PvSystemConfig,
    load_pv_system_config,
    save_pv_system_config,
)
from src.sun_position import sun_position
from src.huawei_client import EnergyState, fetch_energy_state
from src.tariff import (
    cost_breakdown,
    delete_export_rate,
    export_rates_payload,
    group_money_series,
    load_tariff,
    rate_for,
    save_export_rate,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ExportRatePayload(BaseModel):
    """One dated surplus-compensation rate entered in the Energy tab."""

    effective_from: str
    export_eur_kwh: float
    hourly_eur_kwh: Optional[List[Optional[float]]] = None
    replace_effective_from: Optional[str] = None


def _energy_dict(s: EnergyState) -> Dict[str, Any]:
    """Flatten an :class:`EnergyState` into a JSON-serialisable dict."""
    return {
        "grid_import_w": s.grid_import_w,
        "grid_export_w": s.grid_export_w,
        "pv_power_w": s.pv_power_w,
        "house_consumption_w": s.house_consumption_w,
        "pv_surplus_w": s.pv_surplus_w,
        "grid_import_kwh": s.grid_import_kwh,
        "grid_export_kwh": s.grid_export_kwh,
        "meter_reachable": s.meter_reachable,
        "inverter_reachable": s.inverter_reachable,
        "meter_serial": s.meter_serial,
    }


@router.get("/api/energy")
async def get_energy() -> Dict[str, Any]:
    try:
        state = await fetch_energy_state()
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error
        logger.warning("⚠️  Failed to read energy flow: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read energy: {exc}")
    return _energy_dict(state)


@router.get("/api/energy/history")
async def get_energy_history(
    minutes: int = Query(60, ge=1, le=1440),
) -> Dict[str, Any]:
    """Recent raw samples for the live flowing chart.

    ``None`` powers (e.g. asleep PV) are preserved so the client draws a gap,
    never a misleading 0.
    """
    try:
        samples: List[Dict[str, Any]] = recent_samples(minutes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read energy history: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read history: {exc}")
    return {"minutes": minutes, "samples": samples}


@router.get("/api/energy/today")
async def get_energy_today() -> Dict[str, Any]:
    """Today's energy totals (one daily bucket) for the split + savings cards.

    Returns ``{"bucket": <daily bucket>}`` with ``pv_wh`` / ``house_wh`` /
    ``import_wh`` / ``export_wh`` / ``pv_missing`` for the current local day, or
    ``{"bucket": null}`` before any sample has landed today.

    ``gap_hours`` rides alongside (#579): the totals are exactly as measured, so
    a morning the feed spent offline drags them down by real kWh that were in
    fact generated. Reporting the gap is what stops that from being *silent* —
    without it a user reads a dead upstream feed as a dead inverter.
    """
    try:
        buckets = aggregate("daily", 1)
        gap_hours = _feed_gap_hours(hourly_day(0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read today's energy: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to read today: {exc}")
    return {"bucket": buckets[-1] if buckets else None, "gap_hours": gap_hours}


@router.get("/api/energy/aggregate")
async def get_energy_aggregate(
    range: str = Query("day", pattern="^(day|week|month|year|total)$"),
) -> Dict[str, Any]:
    """Calendar-framed energy buckets (Wh) for the history chart.

    Each range is a fixed, fill-up window — 24h ``day``, Mon–Sun ``week``, the
    current ``month``, Jan–Dec ``year``, all-time ``total`` — carrying generation
    / grid-supplied / consumption energy per slot. Future slots come back empty,
    so the chart fills left-to-right. See :func:`framed_buckets`.
    """
    try:
        buckets = framed_buckets(range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to build energy history: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to aggregate: {exc}")
    return {"range": range, "buckets": buckets}


# Nominal day-count per window for prorating the fixed standing charge, capped
# by the actual span of retained history (see :func:`_window_days`) so a young
# history doesn't get charged a full nominal window's worth of fixed cost.
_WINDOW_DAYS = {"day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0}


def _window_days(range_: str, buckets: List[Dict[str, Any]]) -> float:
    """Days the window spans, for prorating fixed costs.

    Capped at the actual span of retained history: while history is younger
    than a range's nominal window (e.g. 23 days of data but a 365-day "year"
    window), "year" would otherwise be charged a full year of fixed cost
    against far less actual consumption — the same inflated-bill bug ``total``
    already avoids by measuring its real span.
    """
    nominal = _WINDOW_DAYS.get(range_)
    if not buckets:  # no history yet
        return 0.0
    first = min(int(b["hour_start"]) for b in buckets)
    span = max(1.0, (time.time() - first) / 86_400.0)
    return min(nominal, span) if nominal is not None else span


@router.get("/api/energy/cost")
async def get_energy_cost(
    range: str = Query("month", pattern="^(day|week|month|year|total)$"),
) -> Dict[str, Any]:
    """Tiered cost & savings breakdown for a window (issue #46).

    Splits the window's hourly energy into time-of-use periods (P1/P2/P3 for a
    Spanish 2.0TD tariff), prices grid import at each period's all-in rate, and
    values self-consumed PV at that same avoided rate (the savings). Returns
    per-period rows + totals + a fixed-cost / estimated-bill summary. Falls back
    to a flat 0.10 €/kWh estimate (``configured: false``) when no tariff is set.
    """
    try:
        buckets = hourly_range(range)
        tariff = load_tariff()
        result = cost_breakdown(buckets, tariff, _window_days(range, buckets))
        result["money_series"] = group_money_series(result["money_series"], range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to build energy cost breakdown: %s", exc)
        raise HTTPException(status_code=502, detail=f"failed to build cost: {exc}")
    result["range"] = range
    return result


@router.get("/api/energy/export-rates")
async def get_export_rates() -> Dict[str, Any]:
    """Dated surplus-compensation rates for the Energy-tab editor."""
    tariff = load_tariff()
    now = datetime.now()
    return {
        "configured": tariff.configured,
        "currency": tariff.currency,
        "current_export_eur_kwh": rate_for(now, tariff),
        "rates": export_rates_payload(tariff),
    }


@router.put("/api/energy/export-rates")
async def update_export_rate(payload: ExportRatePayload) -> Dict[str, Any]:
    """Upsert one effective-dated rate without replacing the tariff document."""
    try:
        tariff = save_export_rate(
            payload.effective_from,
            payload.export_eur_kwh,
            hourly_eur_kwh=payload.hourly_eur_kwh,
            replace_effective_from=payload.replace_effective_from,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = datetime.now()
    return {
        "configured": tariff.configured,
        "currency": tariff.currency,
        "current_export_eur_kwh": rate_for(now, tariff),
        "rates": export_rates_payload(tariff),
    }


@router.delete("/api/energy/export-rates")
async def remove_export_rate(
    effective_from: str = Query(...),
) -> Dict[str, Any]:
    """Delete one dated compensation rate from the editable history."""
    try:
        tariff = delete_export_rate(effective_from)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = datetime.now()
    return {
        "configured": tariff.configured,
        "currency": tariff.currency,
        "current_export_eur_kwh": rate_for(now, tariff),
        "rates": export_rates_payload(tariff),
    }


# Day selector → offset from today, for reading that day's measured generation.
_FORECAST_DAY_OFFSETS = {"yesterday": -1, "today": 0, "tomorrow": 1}


_HOUR_S = 3600

# Below this much of an hour to go on, projecting it to a full-hour rate is
# noise rather than signal: dividing a couple of minutes of samples by a tiny
# window swings the result wildly, and the cloud source itself runs several
# minutes behind wall clock, so the first samples of an hour may not have landed
# at all. Draw a gap for that stretch instead of a wild number.
#
# Applies to both projection bases (#579): the elapsed part of the in-progress
# hour, and the covered part of an hour the feed only partly reached. The
# arithmetic is the same and so is the failure — 5 minutes of data times 12 is
# a guess either way.
_MIN_PROJECTION_ELAPSED_S = 600


def _actual_curve(buckets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A day's measured generation as 24 hourly points (``wh`` ``None`` = no PV).

    ``pv_missing`` hours (asleep inverter, or no sample yet) stay ``None`` so the
    client draws a gap, never a misleading 0 — the same rule the live chart uses.

    Two kinds of hour are *not* comparable to a full hour of expected generation
    and would read as a production collapse if plotted raw:

    * The hour containing *now* is only partly done (issue #557).
    * A settled hour the feed only partly covered (issue #579): its Wh is an
      integral over the minutes that arrived, so a two-thirds outage on a
      cloudless morning plots as near-zero output. Nothing collapsed — the hour
      is **under-measured, not low**, and ``pv_gap`` is how the store says so.

    Both are projected to the rate the hour was actually running at over the
    stretch that *was* measured, and flagged ``estimated`` so the client draws
    them as the inference they are rather than as a measurement. The two bases
    differ deliberately: an in-progress hour scales by how much of it has
    elapsed, a gapped hour by how much of it carried data.

    A fully-covered settled hour is never touched, however low it is — that is
    the normal path and the only one where the raw integral is the truth.
    ``measured_wh`` always carries the untouched measurement either way.

    Takes the day's 24 hourly buckets rather than fetching them, so the caller
    reads the day once and derives both the curve and :func:`_feed_gap_hours`
    from the same snapshot.
    """
    now = int(time.time())
    out: List[Dict[str, Any]] = []
    for i, b in enumerate(buckets):
        measured = None if b["pv_missing"] else b["pv_wh"]
        partial = bool(b.get("partial"))
        gap = bool(b.get("pv_gap"))
        estimated = measured is not None and (partial or gap)
        wh = measured
        if estimated:
            # A gap is the tighter constraint, so it wins when an hour is both:
            # the in-progress hour's elapsed span means nothing if the feed was
            # only up for part of it.
            # Clamped at one hour: an in-progress hour's elapsed span is at most
            # that by definition, and an unclamped subtraction turns any clock
            # disagreement between the store's framing and this one into a
            # silent *division* of a good measurement.
            basis = (
                float(b.get("pv_seconds") or 0.0)
                if gap
                else float(min(_HOUR_S, now - int(b["key"])))
            )
            wh = (
                None
                if basis < _MIN_PROJECTION_ELAPSED_S
                else round(measured * _HOUR_S / basis, 3)
            )
        out.append(
            {
                "hour": i,
                "wh": wh,
                "partial": partial,
                "estimated": estimated,
                "coverage": b.get("pv_coverage"),
                "measured_wh": measured,
            }
        )
    return out


def _feed_gap_hours(buckets: List[Dict[str, Any]]) -> float:
    """How much of a day the PV feed dropped out mid-hour, in hours (#579).

    Sums the uncovered part of every hour that had *some* PV data but not
    enough — the unambiguous outage signature, since data that starts or stops
    mid-hour cannot be a sleeping inverter. Hours with no PV data at all are
    excluded on purpose: overnight they are the normal state, and counting them
    would report a 16-hour "outage" every single day.

    Deliberately conservative and one-directional. This number exists to
    *explain* a depressed day total, never to inflate it — the totals it
    annotates stay exactly as measured.
    """
    total = 0.0
    for b in buckets:
        if b.get("pv_gap"):
            total += max(0.0, _HOUR_S - float(b.get("pv_seconds") or 0.0))
    return round(total / _HOUR_S, 2)


@router.get("/api/energy/forecast")
async def get_energy_forecast(
    day: str = Query("today", pattern="^(yesterday|today|tomorrow)$"),
) -> Dict[str, Any]:
    """Expected-generation forecast curve for a day, with the actual overlay (issue #39).

    Returns the hourly expected-generation curve (Wh) from Open-Meteo's tilted
    irradiance scaled by the configured PV array, the day's expected total (kWh),
    and — for today/yesterday — the measured generation as an overlay (``null``
    for tomorrow, which has no actuals yet). Always 200: ``available=False`` with
    a ``reason`` when the array/location is unconfigured or Open-Meteo is
    unreachable, so the frontend simply keeps the card's "not configured" note.
    """
    try:
        forecast = await fetch_pv_forecast(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — forecast is decorative, never a 500
        logger.warning("⚠️  Failed to build PV forecast: %s", exc)
        return {"available": False, "day": day, "reason": "error"}

    if not forecast.available:
        return {"available": False, "day": day, "reason": forecast.reason}

    # Actuals only exist for days that have already (partly) happened.
    hours = None if day == "tomorrow" else hourly_day(_FORECAST_DAY_OFFSETS[day])
    actual = None if hours is None else _actual_curve(hours)

    return {
        "available": True,
        "day": day,
        "expected": forecast.expected,
        "expected_total_kwh": round(forecast.expected_total_wh / 1000.0, 2),
        "actual": actual,
        # Hours the feed missed, so the card can say the overlay is short
        # because nothing was measured — not because nothing was generated.
        "actual_gap_hours": None if hours is None else _feed_gap_hours(hours),
        "system": forecast.system,  # array params the curve was computed from
    }


# ------------------------------------------- sun-position diagnostic (#590)
# Read-only. Nothing here feeds the forecast: it re-reads what was already
# measured and what the *current* model already predicts, and re-plots the pair
# against where the sun actually was. The point is to tell a fixed obstruction
# (a drop that repeats at the same azimuth every day) from weather (a drop that
# does not), without re-deriving the answer by hand against sqlite each time.

# Irradiance below which an hour carries no usable signal. Expressed as plane-
# of-array W/m² rather than as Wh so it means the same thing on a 1 kWp balcony
# and a 10 kWp roof: at twilight the denominator of the performance ratio goes
# to zero and the ratio explodes, which would swamp the very curve being read.
_MIN_DIAGNOSTIC_GTI_W = 20.0

# Why an hour was left out. Named rather than merely counted, because the three
# mean different things to whoever reads the day: only "coverage" is the feed
# dropping out mid-hour.
_EXCLUDED_COVERAGE = "coverage"      # the feed only reached part of the hour
_EXCLUDED_NO_DATA = "no_data"        # daylight hour with no PV sample at all
_EXCLUDED_IN_PROGRESS = "in_progress"  # the hour containing now isn't over


def _sun_overlay_points(
    buckets: List[Dict[str, Any]],
    expected: List[Dict[str, Any]],
    total_kwp: float,
    performance_ratio: float,
    lat: float,
    lon: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(points, excluded)`` — effective PR per hour against the sun's position.

    The **effective performance ratio** is what the array actually delivered per
    unit of plane-of-array irradiance: ``actual_Wh / (kWp · GTI)``. The modelled
    curve already carries that denominator — ``expected_Wh = kWp · GTI · PR`` —
    so the ratio falls out of the two numbers this app already has, with no
    second irradiance source and no change to the model:

        effective_PR = PR · actual_Wh / expected_Wh

    (For a multi-orientation array the denominator is the kWp-weighted mean GTI
    across sub-arrays, which is the same quantity the forecast sums.)

    Three kinds of hour are dropped, and the *why* matters more than the count:

    * **Short coverage** (:data:`~src.energy_history.MIN_TRUSTED_COVERAGE`) —
      the hour's Wh is an integral over the minutes that arrived, so an hour the
      feed spent half offline plots as half the performance. That is precisely
      the artefact this overlay would otherwise launder into "shading" (#579),
      which is why the coverage signal is consumed here rather than re-derived.
    * **No PV data at all** in a daylight hour — same failure, total rather than
      partial.
    * **The hour containing now** — not finished, so not comparable.

    Hours with no meaningful irradiance (night, deep twilight) are simply not
    plotted: there is nothing to be short of, and they are not a data problem,
    so they are not reported as exclusions either.

    Pure: buckets, the modelled curve and the coordinates all come in as
    arguments, so the whole derivation is unit-testable without a clock, a DB or
    the network.
    """
    expected_by_hour = {
        int(p["hour"]): float(p.get("wh") or 0.0) for p in (expected or [])
    }
    reference = total_kwp * performance_ratio  # Wh per W/m² of GTI

    points: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for i, bucket in enumerate(buckets):
        expected_wh = expected_by_hour.get(i, 0.0)
        gti = expected_wh / reference if reference > 0 else 0.0
        if gti < _MIN_DIAGNOSTIC_GTI_W:
            continue  # dark — no signal to read, and nothing went wrong

        if bool(bucket.get("partial")):
            excluded.append({"hour": i, "reason": _EXCLUDED_IN_PROGRESS})
            continue
        if bucket.get("pv_missing"):
            excluded.append({"hour": i, "reason": _EXCLUDED_NO_DATA})
            continue
        coverage = bucket.get("pv_coverage")
        if coverage is None or float(coverage) < MIN_TRUSTED_COVERAGE:
            excluded.append({"hour": i, "reason": _EXCLUDED_COVERAGE})
            continue

        # Mid-hour is the representative instant: the sun moves ~15°/h, so the
        # hour's own centre is the least-wrong single azimuth for an hour-long
        # energy integral. The bucket key is the hour's start in epoch seconds,
        # which keeps this DST-safe — no local-clock arithmetic anywhere.
        sun = sun_position(int(bucket["key"]) + _HOUR_S // 2, lat, lon)
        actual_wh = float(bucket.get("pv_wh") or 0.0)
        points.append(
            {
                "hour": i,
                "azimuth_deg": sun.azimuth_deg,
                "elevation_deg": sun.elevation_deg,
                "effective_pr": round(performance_ratio * actual_wh / expected_wh, 4),
                "actual_wh": round(actual_wh, 1),
                "expected_wh": round(expected_wh, 1),
                "gti_w_m2": round(gti, 1),
                "coverage": coverage,
            }
        )

    return points, excluded


def _parse_overlay_date(raw: Optional[str], today: date) -> date:
    """The requested day, or today. Raises 400 on anything unusable."""
    if not raw:
        return today
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid date: {raw!r}")
    if parsed > today:
        raise HTTPException(status_code=400, detail="date is in the future")
    return parsed


@router.get("/api/energy/sun-overlay")
async def get_sun_overlay(date_: Optional[str] = Query(None, alias="date")) -> Dict[str, Any]:
    """Measured-vs-modelled performance ratio by sun position, for one day (#590).

    A sibling of ``/api/energy/forecast``, deliberately **not** an extension of
    it: the forecast payload stays byte-identical to what it was, so nothing
    about what the card predicts can drift on the back of a diagnostic.

    Always 200 for a valid, non-future date within Open-Meteo's irradiance
    lookback. A day with no rollup history is an *empty* overlay
    (``points: []``), not an error — the store keeps 400 days of hourly rollups
    but the app has not been running for all of them.
    """
    today = datetime.now().date()
    target = _parse_overlay_date(date_, today)
    if (today - target).days > MAX_PAST_DAYS:
        return {"available": False, "date": target.isoformat(), "reason": "too_old"}

    try:
        forecast = await fetch_pv_forecast_for_date(target, today=today)
    except Exception as exc:  # noqa: BLE001 — diagnostic, never a 500
        logger.warning("⚠️  Failed to build sun-position overlay: %s", exc)
        return {"available": False, "date": target.isoformat(), "reason": "error"}

    # The forecast checks the array before the coordinates, so asking it first
    # keeps the two cards reporting the same reason for the same missing config.
    if not forecast.available:
        return {
            "available": False,
            "date": target.isoformat(),
            "reason": forecast.reason,
        }

    location = load_location_config()
    if location is None:  # unreachable while the forecast is available
        return {"available": False, "date": target.isoformat(), "reason": "no_location"}

    system = forecast.system or {}
    try:
        buckets = hourly_day((target - today).days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️  Failed to read hourly history for the overlay: %s", exc)
        return {"available": False, "date": target.isoformat(), "reason": "error"}

    points, excluded = _sun_overlay_points(
        buckets,
        forecast.expected,
        float(system.get("total_kwp") or 0.0),
        float(system.get("performance_ratio") or 0.0),
        location.lat,
        location.lon,
    )
    return {
        "available": True,
        "date": target.isoformat(),
        # The modelled PR is a single configured number, so it plots as a flat
        # reference the measured points are read against.
        "modelled_pr": system.get("performance_ratio"),
        "points": points,
        "excluded": excluded,
        # Counted apart because they read differently to a human: a partly-
        # covered hour is the feed dropping out mid-hour, a wholly missing one
        # is usually a day the app simply wasn't running for.
        "excluded_coverage": sum(
            1 for e in excluded if e["reason"] == _EXCLUDED_COVERAGE
        ),
        "excluded_no_data": sum(
            1 for e in excluded if e["reason"] == _EXCLUDED_NO_DATA
        ),
        "system": forecast.system,
    }


# --------------------------------------------------------- PV system config
# The write side of the forecast's inputs (issue #561): the Energy tab's "PV
# system" card edits the same gitignored ``config/pv_system.json`` a user may
# still hand-edit. ``src.pv_forecast`` re-reads the file per request, so a save
# is live on the next forecast call — no cache to invalidate, no restart.


class PvArrayPayload(BaseModel):
    """One sub-array as the editor sends it."""

    kwp: float
    tilt_deg: float = 30.0
    azimuth_deg: float = 0.0


class PvHorizonPointPayload(BaseModel):
    """One horizon/shading point (issue #578 part b) as the editor sends it.

    ``azimuth_deg`` is compass, clockwise from true north — see
    :mod:`src.pv_system_config`'s module docstring; deliberately not the same
    convention as ``PvArrayPayload.azimuth_deg``.
    """

    azimuth_deg: float
    elevation_deg: float = 0.0


class PvSystemPayload(BaseModel):
    """A whole-system replace. Every field is optional and omitted-keeps-stored
    so each of the three independent editors (panel rows, performance ratio,
    horizon points) can PUT only what it actually edited."""

    arrays: Optional[List[PvArrayPayload]] = None
    performance_ratio: Optional[float] = None
    horizon_profile: Optional[List[PvHorizonPointPayload]] = None


def _pv_system_payload(config: Optional[PvSystemConfig]) -> Dict[str, Any]:
    """Flatten a config (or "not configured") into the endpoint's JSON shape."""
    if config is None:
        return {
            "configured": False,
            "arrays": [],
            "performance_ratio": PvSystemConfig().performance_ratio,
            "horizon_profile": [],
        }
    return {
        "configured": True,
        "arrays": [
            {"kwp": a.kwp, "tilt_deg": a.tilt_deg, "azimuth_deg": a.azimuth_deg}
            for a in config.arrays
        ],
        "performance_ratio": config.performance_ratio,
        "total_kwp": round(config.total_kwp, 3),
        "horizon_profile": [
            {"azimuth_deg": p.azimuth_deg, "elevation_deg": p.elevation_deg}
            for p in config.horizon_profile
        ],
    }


@router.get("/api/energy/pv-system")
async def get_pv_system() -> Dict[str, Any]:
    """The configured PV array, for the Energy tab's editor (issue #561).

    Always 200: an absent or malformed file is "not configured" — an empty row
    list the editor renders as its empty state — never a 500. A store that
    exists but is transiently unreadable (issue #692) degrades the same way
    here — this is a read-only display, so there is no save to corrupt, unlike
    ``update_pv_system`` below which lets the same failure propagate as a 500.
    """
    try:
        config = load_pv_system_config()
    except StoreUnreadableError as exc:
        logger.warning("⚠️ PV-system config unreadable: %s", exc)
        config = None
    return _pv_system_payload(config)


@router.put("/api/energy/pv-system")
async def update_pv_system(payload: PvSystemPayload) -> Dict[str, Any]:
    """Replace the PV-array config, writing ``config/pv_system.json`` atomically.

    Unlike the read path this one is strict: an invalid row is a 400 naming the
    field, never a silently dropped or clamped value (see
    :func:`src.pv_system_config.validate_pv_system`).

    The editor has no control for the panel-temperature switch (issue #591), but
    it does edit the ratio the switch reinterprets — so the stored switch is
    carried into the validated config. Lowering the ratio back to a combined
    ~0.80 while the term is armed is therefore a 400 explaining the conflict,
    not a saved file that double-counts the thermal loss.

    Three independent editors share this one endpoint (panel rows, performance
    ratio, and the horizon/shading points, issue #578 part b) — each PUTs only
    the field it actually edited; every field omitted-keeps-stored, not just
    ``performance_ratio``, so one editor's save can never wipe another's data.
    The horizon-profile *switch* has no editor control either, same as the
    thermal one — only the points are editable.

    A store that exists but is transiently unreadable (issue #692) is
    deliberately *not* caught here, unlike the GET endpoint above — this is
    "field omitted keeps stored" only because ``stored`` genuinely reflects
    what's on disk. Folding an unreadable read into ``stored=None`` would
    save an omitted field's default over real data, so this 500s instead.
    """
    stored = load_pv_system_config()
    ratio = payload.performance_ratio
    if ratio is None:
        ratio = stored.performance_ratio if stored else PvSystemConfig().performance_ratio

    if payload.arrays is not None:
        arrays = [
            PvArray(kwp=a.kwp, tilt_deg=a.tilt_deg, azimuth_deg=a.azimuth_deg)
            for a in payload.arrays
        ]
    else:
        arrays = list(stored.arrays) if stored else []

    if payload.horizon_profile is not None:
        horizon_profile = [
            PvHorizonPoint(azimuth_deg=p.azimuth_deg, elevation_deg=p.elevation_deg)
            for p in payload.horizon_profile
        ]
    else:
        horizon_profile = list(stored.horizon_profile) if stored else []

    config = PvSystemConfig(
        arrays=arrays,
        performance_ratio=ratio,
        thermal_model_enabled=bool(stored and stored.thermal_model_enabled),
        horizon_profile_enabled=bool(stored and stored.horizon_profile_enabled),
        horizon_profile=horizon_profile,
    )
    try:
        save_pv_system_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _pv_system_payload(config)
