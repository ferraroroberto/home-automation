"""Unit tests for the NOAA solar-position helper (issue #590).

The overlay this feeds reads a *repeating azimuth* as fixed obstruction
geometry, so a systematically wrong azimuth would not look wrong — it would
look like a differently-shaped roof. These tests therefore pin the algorithm
against closed-form astronomy rather than against its own output:

* declination is ±23.44° at the solstices and ~0° at the equinoxes;
* the sun crosses due south (northern hemisphere) or due north (southern) at
  solar noon, and its elevation there is exactly ``90 - |lat - declination|``
  and higher than at any other moment of that day;
* azimuth increases monotonically through the day and never loops.

No clock, config or network: every instant is an explicit UTC timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.sun_position import solar_declination_deg, sun_position

# Kept out of the assertions' way: a couple of real places, chosen only for
# their latitude sign and magnitude.
_MADRID = (40.4168, -3.7038)
_SYDNEY = (-33.8688, 151.2093)


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _day_positions(ts_midnight: float, lat: float, lon: float, step_min: int = 1):
    """Every ``step_min`` minutes of a 24-hour UTC day, as (ts, SunPosition)."""
    return [
        (ts_midnight + m * 60, sun_position(ts_midnight + m * 60, lat, lon))
        for m in range(0, 24 * 60, step_min)
    ]


def _angular_gap(a: float, b: float) -> float:
    """Smallest absolute difference between two azimuths, across the 0/360 seam."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _transit(ts_midnight: float, lat: float, lon: float, meridian_az: float):
    """The instant the sun crosses ``meridian_az``, to the second.

    Deliberately *not* an argmax over elevation: elevation is stationary at
    transit, so with values rounded to two decimals hundreds of consecutive
    seconds tie for the maximum and the argmax lands wherever the scan happens
    to start. The meridian crossing is a sharp event and the correct anchor —
    solar noon by definition.
    """
    coarse_ts, _ = min(
        (p for p in _day_positions(ts_midnight, lat, lon) if p[1].elevation_deg > 0),
        key=lambda p: _angular_gap(p[1].azimuth_deg, meridian_az),
    )
    return min(
        ((coarse_ts + s, sun_position(coarse_ts + s, lat, lon)) for s in range(-90, 91)),
        key=lambda p: _angular_gap(p[1].azimuth_deg, meridian_az),
    )


# ------------------------------------------------------------- declination
def test_declination_peaks_at_the_june_solstice() -> None:
    assert abs(solar_declination_deg(_ts(2026, 6, 21, 12)) - 23.44) < 0.1


def test_declination_troughs_at_the_december_solstice() -> None:
    assert abs(solar_declination_deg(_ts(2026, 12, 21, 12)) + 23.44) < 0.1


def test_declination_crosses_zero_at_the_equinoxes() -> None:
    # The 2026 March equinox falls late on the 20th UTC; noon that day is within
    # a few hours of it, i.e. a fraction of a degree of declination.
    assert abs(solar_declination_deg(_ts(2026, 3, 20, 12))) < 0.3
    assert abs(solar_declination_deg(_ts(2026, 9, 23, 0))) < 0.3


# ---------------------------------------------------------------- elevation
def test_winter_noon_elevation_matches_the_closed_form() -> None:
    """The same identity as the June test, with the declination sign flipped."""
    lat, lon = _MADRID
    transit_ts, transit = _transit(_ts(2026, 12, 21), lat, lon, 180.0)
    expected = 90.0 - abs(lat - solar_declination_deg(transit_ts))
    assert abs(transit.elevation_deg - expected) < 0.02
    # …and that is a genuinely low winter sun, not an arithmetic coincidence.
    assert 25.0 < transit.elevation_deg < 27.0


def test_the_sun_is_below_the_horizon_in_the_middle_of_the_night() -> None:
    lat, lon = _MADRID
    # 01:00 local (≈ 00:00 UTC in winter) — deep night at this longitude.
    assert sun_position(_ts(2026, 12, 21, 0), lat, lon).elevation_deg < -20.0


# ------------------------------------------------------------------ azimuth
def test_northern_hemisphere_meridian_crossing_is_the_day_s_highest_sun() -> None:
    """Crossing due south must coincide with the closed-form peak elevation."""
    lat, lon = _MADRID
    midnight = _ts(2026, 6, 21)
    transit_ts, transit = _transit(midnight, lat, lon, 180.0)
    expected = 90.0 - abs(lat - solar_declination_deg(transit_ts))
    assert abs(transit.elevation_deg - expected) < 0.02
    # …and nothing else in the day beats it, so the crossing really is the peak.
    highest = max(p[1].elevation_deg for p in _day_positions(midnight, lat, lon))
    assert highest <= transit.elevation_deg + 0.01


def test_southern_hemisphere_sun_crosses_due_north() -> None:
    lat, lon = _SYDNEY
    midnight = _ts(2026, 6, 21)
    transit_ts, transit = _transit(midnight, lat, lon, 0.0)
    expected = 90.0 - abs(lat - solar_declination_deg(transit_ts))
    assert abs(transit.elevation_deg - expected) < 0.02
    highest = max(p[1].elevation_deg for p in _day_positions(midnight, lat, lon))
    assert highest <= transit.elevation_deg + 0.01


def test_azimuth_is_east_before_noon_and_west_after() -> None:
    lat, lon = _MADRID
    noon_ts, _ = _transit(_ts(2026, 6, 21), lat, lon, 180.0)
    assert sun_position(noon_ts - 3 * 3600, lat, lon).azimuth_deg < 180.0   # east
    assert sun_position(noon_ts + 3 * 3600, lat, lon).azimuth_deg > 180.0   # west


def test_daylight_azimuth_increases_monotonically() -> None:
    """The signature the overlay reads: azimuth is a clean clock, never a loop."""
    lat, lon = _MADRID
    daylight = [
        pos.azimuth_deg
        for _, pos in _day_positions(_ts(2026, 7, 30), lat, lon, step_min=10)
        if pos.elevation_deg > 0
    ]
    assert len(daylight) > 60  # a real summer day, not an empty sequence
    assert daylight == sorted(daylight)


def test_afternoon_azimuths_span_the_range_the_diagnosis_used() -> None:
    """#578's table runs 165° → 285°; those must be real afternoon azimuths."""
    lat, lon = _MADRID
    reached = {
        round(pos.azimuth_deg)
        for _, pos in _day_positions(_ts(2026, 7, 30), lat, lon, step_min=1)
        if pos.elevation_deg > 0
    }
    for target in (165, 195, 225, 245, 275, 285):
        assert any(abs(a - target) <= 1 for a in reached), target
