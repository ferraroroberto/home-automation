"""API smoke for the read-only sun-position diagnostic (issue #590).

``GET /api/energy/sun-overlay`` is a *sibling* of the forecast endpoint, not an
extension of it — the split exists so a diagnostic can never move what the
Solar forecast card predicts. The most load-bearing test in this file is
therefore the one asserting the forecast payload is untouched.

Open-Meteo is never called: the forecast builder and the hourly store are both
monkeypatched, matching how the rest of this layer keeps itself hermetic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.webapp.routers import energy as E
from src.location_config import LocationConfig
from src.pv_forecast import PvForecast

_HOUR = 3600
_KWP = 4.0
_PR = 0.8

_SYSTEM = {
    "arrays": [{"kwp": _KWP, "tilt_deg": 30.0, "azimuth_deg": 0.0}],
    "total_kwp": _KWP,
    "performance_ratio": _PR,
}


def _local_midnight(offset_days: int = 0) -> int:
    """Epoch seconds of local midnight ``offset_days`` from today."""
    now = datetime.now()
    start = datetime(now.year, now.month, now.day) + timedelta(days=offset_days)
    return int(start.timestamp())


def _modelled_wh(gti_w: float) -> float:
    return gti_w * _KWP * _PR


def _day(offset_days: int, overrides: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    midnight = _local_midnight(offset_days)
    out = []
    for i in range(24):
        slot = {
            "key": str(midnight + i * _HOUR),
            "pv_wh": 0.0, "pv_missing": True, "partial": False,
            "pv_seconds": 0.0, "pv_coverage": 0.0, "pv_gap": False,
        }
        slot.update(overrides.get(i, {}))
        out.append(slot)
    return out


@pytest.fixture(autouse=True)
def _stub_sources(monkeypatch):
    """Location + modelled curve + hourly store, all in-process.

    Returns a mutable dict the test mutates to shape the day: ``hours`` is what
    the store hands back, ``expected`` the modelled curve.
    """
    fixture: Dict[str, Any] = {
        "hours": _day(0, {}),
        "expected": [{"hour": h, "wh": 0.0} for h in range(24)],
        "available": True,
        "reason": None,
    }

    monkeypatch.setattr(
        E, "load_location_config", lambda *a, **k: LocationConfig(lat=40.4168, lon=-3.7038)
    )

    async def fake_forecast(target_day: date, **kwargs: Any) -> PvForecast:
        if not fixture["available"]:
            return PvForecast(available=False, day=target_day.isoformat(),
                              reason=fixture["reason"])
        return PvForecast(
            available=True,
            day=target_day.isoformat(),
            expected=fixture["expected"],
            expected_total_wh=sum(p["wh"] for p in fixture["expected"]),
            system=_SYSTEM,
        )

    monkeypatch.setattr(E, "fetch_pv_forecast_for_date", fake_forecast)
    monkeypatch.setattr(E, "hourly_day", lambda offset: fixture["hours"])
    return fixture


def test_a_fully_covered_hour_plots_its_effective_pr(
    client: TestClient, _stub_sources
) -> None:
    modelled = _modelled_wh(700.0)
    _stub_sources["expected"] = [
        {"hour": h, "wh": modelled if h == 12 else 0.0} for h in range(24)
    ]
    _stub_sources["hours"] = _day(0, {
        12: {"pv_wh": modelled / 2, "pv_missing": False,
             "pv_seconds": float(_HOUR), "pv_coverage": 1.0, "pv_gap": False},
    })

    body = client.get("/api/energy/sun-overlay").json()

    assert body["available"] is True
    assert body["modelled_pr"] == _PR
    assert body["excluded"] == []
    assert body["excluded_coverage"] == 0
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["hour"] == 12
    assert point["effective_pr"] == pytest.approx(_PR / 2)
    assert 0.0 < point["azimuth_deg"] < 360.0


def test_a_short_coverage_hour_is_reported_as_excluded_not_plotted(
    client: TestClient, _stub_sources
) -> None:
    modelled = _modelled_wh(700.0)
    _stub_sources["expected"] = [
        {"hour": h, "wh": modelled if h == 10 else 0.0} for h in range(24)
    ]
    _stub_sources["hours"] = _day(0, {
        10: {"pv_wh": modelled / 4, "pv_missing": False,
             "pv_seconds": 900.0, "pv_coverage": 0.25, "pv_gap": True},
    })

    body = client.get("/api/energy/sun-overlay").json()

    assert body["points"] == []
    assert body["excluded"] == [{"hour": 10, "reason": "coverage"}]
    assert body["excluded_coverage"] == 1
    assert body["excluded_no_data"] == 0


def test_a_day_with_no_history_is_an_empty_overlay_not_an_error(
    client: TestClient, _stub_sources
) -> None:
    """400 days of retention, but the app has not been running for all of them."""
    modelled = _modelled_wh(700.0)
    _stub_sources["expected"] = [
        {"hour": h, "wh": modelled if 9 <= h <= 15 else 0.0} for h in range(24)
    ]
    _stub_sources["hours"] = _day(-30, {})   # every hour empty

    resp = client.get("/api/energy/sun-overlay?date=%s"
                      % (date.today() - timedelta(days=30)).isoformat())

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["points"] == []
    # Daylight hours with no data at all are still *named*, so the card can say
    # the overlay is empty because nothing was measured — and counted apart
    # from a feed that dropped out mid-hour, which means something different.
    assert {e["reason"] for e in body["excluded"]} == {"no_data"}
    assert body["excluded_coverage"] == 0
    assert body["excluded_no_data"] == len(body["excluded"]) > 0


def test_an_unconfigured_array_reports_the_forecast_s_own_reason(
    client: TestClient, _stub_sources
) -> None:
    _stub_sources["available"] = False
    _stub_sources["reason"] = "not_configured"
    body = client.get("/api/energy/sun-overlay").json()
    assert body == {
        "available": False,
        "date": date.today().isoformat(),
        "reason": "not_configured",
    }


def test_a_future_date_is_a_400(client: TestClient) -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert client.get("/api/energy/sun-overlay?date=%s" % tomorrow).status_code == 400


def test_a_malformed_date_is_a_400(client: TestClient) -> None:
    assert client.get("/api/energy/sun-overlay?date=not-a-date").status_code == 400


def test_a_date_beyond_the_irradiance_lookback_is_reported_not_fetched(
    client: TestClient, _stub_sources
) -> None:
    old = (date.today() - timedelta(days=400)).isoformat()
    body = client.get("/api/energy/sun-overlay?date=%s" % old).json()
    assert body["available"] is False
    assert body["reason"] == "too_old"


def test_the_forecast_endpoint_payload_is_untouched(
    client: TestClient, monkeypatch
) -> None:
    """The split's whole reason for being: the forecast keeps its exact shape."""
    async def fake_named(day: str, **kwargs: Any) -> PvForecast:
        return PvForecast(
            available=True, day=day,
            expected=[{"hour": 12, "wh": 2000.0}],
            expected_total_wh=2000.0,
            system=_SYSTEM,
        )

    monkeypatch.setattr(E, "fetch_pv_forecast", fake_named)
    monkeypatch.setattr(E, "hourly_day", lambda offset: _day(0, {}))

    body = client.get("/api/energy/forecast?day=today").json()

    assert sorted(body) == [
        "actual", "actual_gap_hours", "available", "day",
        "expected", "expected_total_kwh", "system",
    ]
