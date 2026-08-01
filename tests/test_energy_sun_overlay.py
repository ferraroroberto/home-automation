"""Unit tests for the measured-vs-modelled sun-position overlay (issue #590).

The overlay's whole value is that a repeating drop at a repeating *azimuth* is
evidence of fixed obstruction geometry. That evidence is only worth having if
an under-measured hour can never masquerade as a shaded one — so the exclusion
rules get as much attention here as the arithmetic does.

No clock, DB, cloud or FastAPI: the day's buckets, the modelled curve and the
coordinates are all passed in.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.webapp.routers import energy as E
from src.energy_history import MIN_TRUSTED_COVERAGE

# 2026-07-30 00:00 UTC — the day #578's diagnosis table was derived from.
_DAY_START = 1_785_369_600
_HOUR = 3600

_MADRID_LAT, _MADRID_LON = 40.4168, -3.7038

# A 4 kWp array at the usual 0.80 derate: 1 W/m² of GTI is worth 3.2 Wh.
_KWP = 4.0
_PR = 0.80


def _day(overrides: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A 24-slot day frame of settled, empty hours with per-hour overrides.

    Mirrors :func:`src.energy_history.hourly_day`, coverage fields included.
    """
    out = []
    for i in range(24):
        slot = {
            "key": str(_DAY_START + i * _HOUR),
            "pv_wh": 0.0,
            "pv_missing": True,
            "partial": False,
            "pv_seconds": 0.0,
            "pv_coverage": 0.0,
            "pv_gap": False,
        }
        slot.update(overrides.get(i, {}))
        out.append(slot)
    return out


def _covered(wh: float, **extra: Any) -> Dict[str, Any]:
    """A fully-covered hour that measured ``wh`` — the normal path."""
    slot = {
        "pv_wh": wh, "pv_missing": False,
        "pv_seconds": float(_HOUR), "pv_coverage": 1.0, "pv_gap": False,
    }
    slot.update(extra)
    return slot


def _expected(**by_hour: float) -> List[Dict[str, Any]]:
    """The modelled curve, as ``{"hour": h, "wh": …}`` rows keyed ``h<N>``."""
    return [
        {"hour": int(name[1:]), "wh": wh} for name, wh in sorted(by_hour.items())
    ]


def _overlay(buckets, expected, kwp: float = _KWP, pr: float = _PR):
    return E._sun_overlay_points(
        buckets, expected, kwp, pr, _MADRID_LAT, _MADRID_LON
    )


def _gti_wh(gti_w: float, kwp: float = _KWP, pr: float = _PR) -> float:
    """The modelled Wh a given plane-of-array irradiance implies."""
    return gti_w * kwp * pr


# ------------------------------------------------------------- the arithmetic
def test_effective_pr_is_actual_over_the_modelled_irradiance_term() -> None:
    """A perfect hour reads back exactly the configured PR."""
    modelled = _gti_wh(700.0)                      # 2240 Wh at 4 kWp · PR 0.80
    points, excluded = _overlay(
        _day({12: _covered(modelled)}), _expected(h12=modelled)
    )

    assert excluded == []
    assert len(points) == 1
    assert points[0]["hour"] == 12
    assert points[0]["effective_pr"] == pytest.approx(_PR)
    assert points[0]["gti_w_m2"] == pytest.approx(700.0)


def test_half_the_modelled_generation_halves_the_effective_pr() -> None:
    modelled = _gti_wh(600.0)
    points, _ = _overlay(
        _day({15: _covered(modelled / 2)}), _expected(h15=modelled)
    )
    assert points[0]["effective_pr"] == pytest.approx(_PR / 2)


def test_the_measurement_rides_along_untouched() -> None:
    """Nothing here is projected or scaled — this is the raw integral."""
    modelled = _gti_wh(500.0)
    points, _ = _overlay(
        _day({11: _covered(1234.5)}), _expected(h11=modelled)
    )
    assert points[0]["actual_wh"] == pytest.approx(1234.5)
    assert points[0]["expected_wh"] == pytest.approx(modelled)


# ------------------------------------------- the exclusions that matter (#579)
def test_a_short_coverage_hour_is_excluded_not_plotted_as_a_low_pr() -> None:
    """The failure this overlay exists not to commit.

    2026-07-30 10:00 covered half the hour on a cloudless morning. Plotted raw
    it is a 50% performance ratio at an afternoon azimuth — indistinguishable
    from shading, and it would be baked into a horizon profile.
    """
    modelled = _gti_wh(650.0)
    buckets = _day({
        10: _covered(modelled / 2, pv_coverage=0.5, pv_seconds=1800.0, pv_gap=True),
    })
    points, excluded = _overlay(buckets, _expected(h10=modelled))

    assert points == []
    assert excluded == [{"hour": 10, "reason": "coverage"}]


def test_a_zero_wh_outage_hour_is_excluded_even_though_pv_gap_is_false() -> None:
    """The gap flag alone is not enough, by its own definition.

    ``pv_gap`` deliberately skips hours that measured 0 Wh — overnight that is
    an asleep inverter, not an outage. But in *daylight* a 0 Wh hour on a
    quarter of the samples is exactly the dead feed, and would plot as an
    effective PR of zero: the strongest possible false shading signal. Coverage
    is what has to be consulted, not the flag derived from it.
    """
    modelled = _gti_wh(700.0)
    buckets = _day({
        9: _covered(0.0, pv_coverage=0.25, pv_seconds=900.0, pv_gap=False),
    })
    points, excluded = _overlay(buckets, _expected(h9=modelled))

    assert points == []
    assert excluded == [{"hour": 9, "reason": "coverage"}]


def test_a_daylight_hour_with_no_pv_data_at_all_is_excluded() -> None:
    modelled = _gti_wh(500.0)
    points, excluded = _overlay(_day({}), _expected(h13=modelled))
    assert points == []
    assert excluded == [{"hour": 13, "reason": "no_data"}]


def test_the_in_progress_hour_is_excluded() -> None:
    """Not finished, so not comparable to a full hour of modelled generation."""
    modelled = _gti_wh(600.0)
    buckets = _day({14: _covered(100.0, partial=True, pv_coverage=1.0)})
    points, excluded = _overlay(buckets, _expected(h14=modelled))
    assert points == []
    assert excluded == [{"hour": 14, "reason": "in_progress"}]


def test_an_hour_just_over_the_coverage_line_is_still_plotted() -> None:
    """The normal path must not be collateral damage of the guard."""
    modelled = _gti_wh(600.0)
    buckets = _day({12: _covered(modelled, pv_coverage=MIN_TRUSTED_COVERAGE)})
    points, excluded = _overlay(buckets, _expected(h12=modelled))
    assert excluded == []
    assert len(points) == 1


def test_a_genuinely_dim_but_fully_covered_hour_is_plotted_as_low() -> None:
    """Real weather still has to show up — the guard is about coverage only."""
    modelled = _gti_wh(600.0)
    buckets = _day({12: _covered(modelled * 0.2)})
    points, excluded = _overlay(buckets, _expected(h12=modelled))
    assert excluded == []
    assert points[0]["effective_pr"] == pytest.approx(_PR * 0.2)


# --------------------------------------------------------------- dark hours
def test_night_hours_are_neither_plotted_nor_reported_as_exclusions() -> None:
    """Nothing was measurable and nothing went wrong — so, no noise."""
    points, excluded = _overlay(_day({}), _expected())
    assert points == []
    assert excluded == []


def test_deep_twilight_is_below_the_irradiance_floor() -> None:
    """Dividing by a near-zero denominator would swamp the whole curve."""
    faint = _gti_wh(5.0)   # 5 W/m² — under the floor
    buckets = _day({6: _covered(0.0, pv_coverage=0.1)})
    points, excluded = _overlay(buckets, _expected(h6=faint))
    assert points == []
    assert excluded == []


def test_an_unconfigured_array_yields_no_points_rather_than_dividing_by_zero() -> None:
    modelled = _gti_wh(700.0)
    points, excluded = _overlay(
        _day({12: _covered(modelled)}), _expected(h12=modelled), kwp=0.0
    )
    assert points == []
    assert excluded == []


# --------------------------------------------------------------- sun position
def test_points_carry_a_plausible_sun_position_for_their_hour() -> None:
    """Morning east of the meridian, afternoon west — the axis of the whole card."""
    modelled = _gti_wh(600.0)
    buckets = _day({8: _covered(modelled), 16: _covered(modelled)})
    points, _ = _overlay(buckets, _expected(h8=modelled, h16=modelled))

    by_hour = {p["hour"]: p for p in points}
    assert by_hour[8]["azimuth_deg"] < 180.0
    assert by_hour[16]["azimuth_deg"] > 180.0
    assert by_hour[8]["elevation_deg"] > 0.0
    assert by_hour[16]["elevation_deg"] > 0.0


def test_azimuth_rises_with_the_hour_across_the_day() -> None:
    modelled = _gti_wh(600.0)
    hours = [8, 10, 12, 14, 16]
    buckets = _day({h: _covered(modelled) for h in hours})
    points, _ = _overlay(
        buckets, _expected(**{"h%d" % h: modelled for h in hours})
    )
    azimuths = [p["azimuth_deg"] for p in points]
    assert [p["hour"] for p in points] == hours
    assert azimuths == sorted(azimuths)
