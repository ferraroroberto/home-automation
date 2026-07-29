"""Unit tests for the forecast card's *actual* generation curve (issue #557).

The hour containing *now* is only partly done, so plotting its accumulated Wh
against a full hour of expected generation reads as a production collapse — the
array is still running, the hour simply isn't over. These tests pin the
projection that fixes it and, just as importantly, the guard rails that stop it
from inventing generation that never happened.

No clock, DB or cloud: ``hourly_day`` and the module's clock are both injected.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from app.webapp.routers import energy as E

_DAY_START = 1_699_920_000  # a whole hour, standing in for local midnight
_HOUR = 3600


def _day(overrides: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A 24-slot day frame of empty settled hours, with per-hour overrides."""
    out = []
    for i in range(24):
        slot = {
            "key": str(_DAY_START + i * _HOUR),
            "pv_wh": 0.0,
            "pv_missing": True,
            "partial": False,
        }
        slot.update(overrides.get(i, {}))
        out.append(slot)
    return out


def _curve(monkeypatch, slots: List[Dict[str, Any]], now: int) -> List[Dict[str, Any]]:
    monkeypatch.setattr(E, "hourly_day", lambda offset_days: slots)
    monkeypatch.setattr(E, "time", SimpleNamespace(time=lambda: now))
    return E._actual_curve(0)


def test_the_in_progress_hour_is_projected_to_a_full_hour_rate(monkeypatch) -> None:
    """Half an hour in at 1500 Wh is running at 3000 Wh/h — plot that, not 1500."""
    slots = _day({10: {"pv_wh": 1500.0, "pv_missing": False, "partial": True}})
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 1800)

    assert len(curve) == 24
    point = curve[10]
    assert point["hour"] == 10
    assert point["partial"] is True
    assert point["wh"] == 3000.0
    assert point["measured_wh"] == 1500.0  # the measurement is never overwritten


def test_a_settled_hour_with_a_gap_is_never_scaled_up(monkeypatch) -> None:
    """An hour that is short because the cloud feed was down really was that low."""
    slots = _day({
        9: {"pv_wh": 400.0, "pv_missing": False},  # settled, short — an outage
        10: {"pv_wh": 1500.0, "pv_missing": False, "partial": True},
    })
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 1800)

    assert curve[9]["partial"] is False
    assert curve[9]["wh"] == 400.0  # untouched, not projected to 3600
    assert curve[10]["wh"] == 3000.0


def test_no_projection_in_the_opening_minutes_of_an_hour(monkeypatch) -> None:
    """Dividing a few minutes of samples by a tiny window is noise — draw a gap."""
    slots = _day({10: {"pv_wh": 50.0, "pv_missing": False, "partial": True}})
    curve = _curve(monkeypatch, slots, _DAY_START + 10 * _HOUR + 300)

    assert curve[10]["wh"] is None
    assert curve[10]["measured_wh"] == 50.0


def test_the_projection_threshold_is_inclusive(monkeypatch) -> None:
    """Exactly at the cutoff the hour projects, so there is no dead minute."""
    slots = _day({10: {"pv_wh": 600.0, "pv_missing": False, "partial": True}})
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


def test_a_past_day_has_no_projected_hour(monkeypatch) -> None:
    """Yesterday is entirely settled — every point is the measured value."""
    slots = _day({9: {"pv_wh": 400.0, "pv_missing": False}})
    curve = _curve(monkeypatch, slots, _DAY_START + 30 * _HOUR)

    assert not any(p["partial"] for p in curve)
    assert curve[9]["wh"] == 400.0
