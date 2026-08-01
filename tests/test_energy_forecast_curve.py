"""Unit tests for the forecast card's *actual* generation curve (issues #557, #579).

Two ways an hour can be plotted as a production collapse when nothing collapsed:

* it is still in progress, so its accumulated Wh is not yet a full hour (#557);
* it is settled but the feed only covered part of it, so its Wh is an integral
  over the minutes that arrived (#579).

These tests pin the projection that fixes both and, just as importantly, the
guard rails that stop it from inventing generation that never happened.

No clock, DB or cloud: the day's buckets are passed in and the module's clock is
injected.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from app.webapp.routers import energy as E

_DAY_START = 1_699_920_000  # a whole hour, standing in for local midnight
_HOUR = 3600


def _day(overrides: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A 24-slot day frame of empty settled hours, with per-hour overrides.

    Mirrors what :func:`src.energy_history.hourly_day` returns, coverage fields
    included — an hour that carries PV is fully covered unless a case says
    otherwise.
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


def _curve(monkeypatch, slots: List[Dict[str, Any]], now: int) -> List[Dict[str, Any]]:
    monkeypatch.setattr(E, "time", SimpleNamespace(time=lambda: now))
    return E._actual_curve(slots)


# ------------------------------------------------- the in-progress hour (#557)
def test_the_in_progress_hour_is_projected_to_a_full_hour_rate(monkeypatch) -> None:
    """Half an hour in at 1500 Wh is running at 3000 Wh/h — plot that, not 1500."""
    slots = _day({10: _covered(1500.0, partial=True, pv_seconds=1800.0)})
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 1800)

    assert len(curve) == 24
    point = curve[10]
    assert point["hour"] == 10
    assert point["partial"] is True
    assert point["estimated"] is True
    assert point["wh"] == 3000.0
    assert point["measured_wh"] == 1500.0  # the measurement is never overwritten


def test_no_projection_in_the_opening_minutes_of_an_hour(monkeypatch) -> None:
    """Dividing a few minutes of samples by a tiny window is noise — draw a gap."""
    slots = _day({10: _covered(50.0, partial=True, pv_seconds=300.0)})
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 300)

    assert curve[10]["wh"] is None
    assert curve[10]["measured_wh"] == 50.0


def test_the_projection_threshold_is_inclusive(monkeypatch) -> None:
    """Exactly at the cutoff the hour projects, so there is no dead minute."""
    slots = _day({10: _covered(600.0, partial=True, pv_seconds=600.0)})
    curve = _curve(
        monkeypatch, slots, _DAY_START + 10 * _HOUR + E._MIN_PROJECTION_ELAPSED_S
    )

    assert curve[10]["wh"] == 3600.0


def test_a_partial_hour_with_no_pv_sample_stays_a_gap(monkeypatch) -> None:
    """An asleep inverter must not be projected up from a missing reading."""
    slots = _day({10: {"partial": True}})  # pv_missing stays True
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 1800)

    assert curve[10]["wh"] is None
    assert curve[10]["measured_wh"] is None
    assert curve[10]["estimated"] is False


def test_a_past_day_has_no_projected_hour(monkeypatch) -> None:
    """Yesterday is entirely settled and fully covered — every point is measured."""
    slots = _day({9: _covered(400.0)})
    curve = _curve(monkeypatch, slots, _DAY_START + 30 * _HOUR)

    assert not any(p["partial"] for p in curve)
    assert not any(p["estimated"] for p in curve)
    assert curve[9]["wh"] == 400.0


# ------------------------------------------------ partial-coverage hours (#579)
def test_a_settled_fully_covered_hour_is_never_scaled_up(monkeypatch) -> None:
    """A genuinely dim hour really was that low — the normal path, untouched."""
    slots = _day({9: _covered(400.0)})
    curve = _curve(monkeypatch, slots, _DAY_START + 30 * _HOUR)

    assert curve[9]["estimated"] is False
    assert curve[9]["wh"] == 400.0  # not projected to 3600
    assert curve[9]["coverage"] == 1.0


def test_a_settled_hour_the_feed_half_missed_is_projected_not_plotted_flat(
    monkeypatch,
) -> None:
    """The reported symptom: 30 of 60 minutes of data is under-measured, not low."""
    slots = _day({
        10: {
            "pv_wh": 2048.0, "pv_missing": False,
            "pv_seconds": 1800.0, "pv_coverage": 0.5, "pv_gap": True,
        },
    })
    curve = _curve(monkeypatch, slots, _DAY_START + 30 * _HOUR)

    point = curve[10]
    assert point["partial"] is False       # the hour is over; it is not in progress
    assert point["estimated"] is True      # but it is an inference, not a measurement
    assert point["wh"] == 4096.0           # the rate the covered half was running at
    assert point["measured_wh"] == 2048.0  # the raw integral is still reported
    assert point["coverage"] == 0.5


def test_a_barely_covered_hour_draws_a_gap_rather_than_a_wild_number(
    monkeypatch,
) -> None:
    """Under ten minutes of data cannot support a full-hour rate — say nothing."""
    slots = _day({
        9: {
            "pv_wh": 30.0, "pv_missing": False,
            "pv_seconds": 300.0, "pv_coverage": 0.083, "pv_gap": True,
        },
    })
    curve = _curve(monkeypatch, slots, _DAY_START + 30 * _HOUR)

    assert curve[9]["wh"] is None          # a gap, not a 360 Wh guess
    assert curve[9]["measured_wh"] == 30.0
    assert curve[9]["estimated"] is True


def test_a_gap_in_the_in_progress_hour_scales_by_coverage_not_by_elapsed(
    monkeypatch,
) -> None:
    """Both flags at once: elapsed time means nothing if the feed was down for it."""
    slots = _day({
        10: {
            "pv_wh": 1000.0, "pv_missing": False, "partial": True,
            "pv_seconds": 900.0, "pv_coverage": 0.5, "pv_gap": True,
        },
    })
    # 30 min elapsed, but only 15 min of it carried data.
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 1800)

    assert curve[10]["wh"] == 4000.0  # 1000 * 3600/900, not 1000 * 3600/1800


# ----------------------------------------------------- the day's feed gap (#579)
def test_feed_gap_hours_sums_only_partly_covered_hours() -> None:
    """The reported day: 09:00 at 25% and 10:00 at 50% coverage → 1.25 h missing."""
    slots = _day({
        9: {"pv_wh": 400.0, "pv_missing": False,
            "pv_seconds": 900.0, "pv_coverage": 0.25, "pv_gap": True},
        10: {"pv_wh": 2048.0, "pv_missing": False,
             "pv_seconds": 1800.0, "pv_coverage": 0.5, "pv_gap": True},
        12: _covered(4000.0),
    })
    assert E._feed_gap_hours(slots) == 1.25


def test_feed_gap_hours_never_counts_the_night() -> None:
    """Every hour of an untouched day is uncovered — none of it is an outage."""
    assert E._feed_gap_hours(_day({})) == 0.0
