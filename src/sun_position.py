"""Where the sun was, for the PV diagnostic overlay (issue #590).

Turns an instant + the home's coordinates into the sun's **azimuth** and
**elevation**. That is the only thing this module does: it is the missing
x-axis for plotting measured generation as *geometry* rather than as a
time-of-day mystery. A performance drop that repeats at the same azimuth
across days is a fixed obstruction; one that wanders with the clock is not.

Implementation is NOAA's low-precision solar-position algorithm (the one behind
their public solar calculator), which is accurate to well under a tenth of a
degree over the years this app has history for — far finer than the hourly
resolution it is asked for here. Deliberately stdlib-only: the alternative was
a new runtime dependency for ~60 lines of closed-form trigonometry.

Two conventions worth stating, because both have a defensible opposite:

* **Azimuth is measured clockwise from true north** — 0° N, 90° E, 180° S,
  270° W. That is NOAA's convention and the one PV datasheets use for panel
  orientation, so a sub-array's ``azimuth_deg`` and a sun azimuth are directly
  comparable. (``config/pv_system.json`` stores its own azimuth as an offset
  from south, per :mod:`src.pv_system_config` — the two are not the same
  number and are never mixed here.)
* **Elevation is geometric, not apparent** — no atmospheric-refraction term.
  Refraction only matters within roughly half a degree of the horizon, and the
  overlay excludes those hours anyway (there is no usable irradiance in them),
  so the correction would add error-prone code that never changes an answer.

UI-free: imported by the energy API, never imports the UI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Julian day of the J2000.0 epoch, and of the Unix epoch — the two anchors that
# turn an epoch timestamp into the Julian centuries the algorithm is written in.
_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class SunPosition:
    """The sun's apparent place in the sky at one instant, from one place."""

    # Clockwise from true north: 0 = N, 90 = E, 180 = S, 270 = W.
    azimuth_deg: float
    # Above the horizon; negative when the sun is down.
    elevation_deg: float


def _julian_centuries(ts: float) -> float:
    """Julian centuries since J2000.0 for a Unix timestamp (seconds, UTC)."""
    return ((ts / _SECONDS_PER_DAY + _UNIX_EPOCH_JD) - _J2000) / 36525.0


def _clamp_unit(x: float) -> float:
    """Clamp to [-1, 1] so float drift can't hand ``acos`` a domain error."""
    return max(-1.0, min(1.0, x))


def solar_declination_deg(ts: float) -> float:
    """The sun's declination in degrees at ``ts`` (Unix seconds, UTC).

    Exposed on its own because it is the one term with a textbook value to
    check against (±23.44° at the solstices, ~0° at the equinoxes), which is
    what pins the rest of the algorithm in tests.
    """
    return _solar_terms(ts)[0]


def _solar_terms(ts: float) -> tuple:
    """``(declination_deg, equation_of_time_minutes)`` at ``ts``."""
    t = _julian_centuries(ts)

    # Geometric mean longitude and mean anomaly of the sun.
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m = math.radians(mean_anom)
    equation_of_centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = mean_long + equation_of_centre
    # Apparent longitude: the nutation/aberration correction.
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(omega)

    mean_obliquity = 23.0 + (
        26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0
    ) / 60.0
    obliquity = math.radians(mean_obliquity + 0.00256 * math.cos(omega))

    declination = math.degrees(
        math.asin(_clamp_unit(math.sin(obliquity) * math.sin(math.radians(apparent_long))))
    )

    # Equation of time (minutes): how far true solar time runs ahead of mean.
    vary = math.tan(obliquity / 2.0) ** 2
    l0 = math.radians(mean_long)
    eot = 4.0 * math.degrees(
        vary * math.sin(2 * l0)
        - 2 * eccentricity * math.sin(m)
        + 4 * eccentricity * vary * math.sin(m) * math.cos(2 * l0)
        - 0.5 * vary * vary * math.sin(4 * l0)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m)
    )
    return declination, eot


def sun_position(ts: float, lat: float, lon: float) -> SunPosition:
    """The sun's azimuth + elevation at Unix time ``ts`` seen from ``lat``/``lon``.

    ``ts`` is epoch seconds (UTC — an epoch timestamp carries no timezone, so
    the caller's local clock never enters the arithmetic). ``lon`` is positive
    east of Greenwich, matching ``config/location.json``.
    """
    declination_deg, eot_minutes = _solar_terms(ts)

    # Minutes past UTC midnight, then true solar time at this longitude.
    minutes_utc = (ts % _SECONDS_PER_DAY) / 60.0
    true_solar_minutes = (minutes_utc + eot_minutes + 4.0 * lon) % 1440.0
    hour_angle_deg = true_solar_minutes / 4.0 - 180.0

    lat_r = math.radians(lat)
    dec_r = math.radians(declination_deg)
    ha_r = math.radians(hour_angle_deg)

    cos_zenith = _clamp_unit(
        math.sin(lat_r) * math.sin(dec_r)
        + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)
    )
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)

    denominator = math.cos(lat_r) * math.sin(zenith)
    if abs(denominator) < 1e-9:
        # Sun exactly overhead, or an observer at a pole: azimuth is undefined.
        # Report the meridian the sun is on, which is the only stable answer and
        # keeps the value plottable instead of NaN.
        azimuth = 180.0 if lat >= 0 else 0.0
    else:
        core = math.degrees(
            math.acos(_clamp_unit((math.sin(lat_r) * cos_zenith - math.sin(dec_r)) / denominator))
        )
        # Before solar noon the sun is east of the meridian, after it west.
        azimuth = (core + 180.0) % 360.0 if hour_angle_deg > 0 else (540.0 - core) % 360.0

    return SunPosition(
        azimuth_deg=round(azimuth, 2),
        elevation_deg=round(elevation, 2),
    )
