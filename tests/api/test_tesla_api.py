"""API smoke for the Tesla read routes.

``GET /api/tesla``, ``/api/tesla/history`` and ``/api/tesla/sessions`` serve
the local store only: a request never reaches the Fleet API, and no route
under the prefix accepts anything but GET.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.devices import tesla_client, tesla_history
from src.devices.tesla_client import TeslaSample


def _sample(ts: int, **over) -> TeslaSample:
    base = dict(ts=ts, battery_pct=80.0, range_km=400.0, charging_state="Disconnected",
                charger_power_kw=0.0, charge_energy_added_kwh=0.0, odometer_km=1000.0,
                shift_state=None, speed_kmh=None, at_home=True)
    base.update(over)
    return TeslaSample(**base)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp store, the car reported as configured, and Tesla unreachable."""
    db = tmp_path / "tesla_history.sqlite3"
    monkeypatch.setattr(tesla_history, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.devices.tesla_auth.is_configured", lambda _cfg: True)

    async def _no_cloud(**_kw):
        raise AssertionError("a read route called the Fleet API")

    monkeypatch.setattr(tesla_client, "read_vehicle", _no_cloud)
    return db


def test_every_tesla_route_is_get_only() -> None:
    from app.webapp.routers.tesla import router

    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(router.routes)
    assert {r.path for r in routes} == {"/api/tesla", "/api/tesla/history", "/api/tesla/sessions"}
    for route in routes:
        assert route.methods <= {"GET", "HEAD"}, f"{route.path} accepts {route.methods}"


@pytest.mark.parametrize("path", ["/api/tesla", "/api/tesla/history", "/api/tesla/sessions"])
def test_post_to_tesla_is_refused(client: TestClient, store: Path, path: str) -> None:
    assert client.post(path).status_code == 405


def test_unconfigured_reports_hidden_and_opens_no_store(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "tesla_history.sqlite3"
    monkeypatch.setattr(tesla_history, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.devices.tesla_auth.is_configured", lambda _cfg: False)

    body = client.get("/api/tesla").json()
    assert body["configured"] is False
    assert body["sample"] is None
    history = client.get("/api/tesla/history").json()
    assert (history["configured"], history["period"], history["buckets"]) == (False, "week", [])
    assert client.get("/api/tesla/sessions").json() == {"configured": False, "drives": [], "charging": []}
    assert not db.exists()


def test_configured_with_no_data_yet(client: TestClient, store: Path) -> None:
    body = client.get("/api/tesla").json()
    assert body["configured"] is True
    assert body["sample"] is None
    assert body["state"] is None
    assert body["usable_kwh"] > 0


def test_latest_sample_and_state_come_from_the_store(client: TestClient, store: Path) -> None:
    tesla_history.init_db()
    tesla_history.record_sample(_sample(1_700_000_000, battery_pct=71.0, range_km=355.5))
    tesla_history.record_status("asleep", "vehicle data returned HTTP 500", ts=1_700_000_600)

    body = client.get("/api/tesla").json()
    assert body["sample"]["battery_pct"] == 71.0
    assert body["sample"]["range_km"] == 355.5
    assert body["state"] == "asleep"
    assert body["checked_at"] == 1_700_000_600
    assert "error" not in body



# Parked at home, a 25 km drive using 10 % (6 kWh), then a 9.1 kWh charge at
# home closed by a Complete read: one drive and one charging session.
_T0 = 1_760_000_000


def _seed_drive_and_charge() -> None:
    tesla_history.init_db()
    seq = [
        _sample(_T0),
        _sample(_T0 + 120, shift_state="D", speed_kmh=40.0, battery_pct=79.0, odometer_km=1001.0),
        _sample(_T0 + 1440, shift_state="P", battery_pct=70.0, odometer_km=1025.0),
        _sample(_T0 + 3600, battery_pct=70.0, odometer_km=1025.0, charging_state="Charging",
                charger_power_kw=7.4, charge_energy_added_kwh=0.5),
        _sample(_T0 + 8880, battery_pct=85.0, odometer_km=1025.0, charging_state="Complete",
                charge_energy_added_kwh=9.1),
    ]
    for s in seq:
        tesla_history.record_sample(s)
    tesla_history.refresh_sessions(tesla_history.TeslaHistoryConfig(usable_kwh=60.0))


@pytest.mark.parametrize("period", ["week", "month"])
def test_history_buckets_come_from_the_store(client: TestClient, store: Path, period: str) -> None:
    _seed_drive_and_charge()

    body = client.get(f"/api/tesla/history?period={period}").json()
    assert body["configured"] is True and body["period"] == period
    [bucket] = body["buckets"]
    assert bucket["km"] == pytest.approx(25.0)
    assert bucket["charged_kwh"] == pytest.approx(9.1)
    assert bucket["charged_home_kwh"] == pytest.approx(9.1)
    assert bucket["kwh_per_100km"] == pytest.approx(24.0)


def test_history_rejects_an_unknown_period(client: TestClient, store: Path) -> None:
    assert client.get("/api/tesla/history?period=year").status_code == 422


def test_sessions_come_from_the_store_without_location_detail(client: TestClient, store: Path) -> None:
    _seed_drive_and_charge()

    body = client.get("/api/tesla/sessions?limit=5").json()
    assert body["configured"] is True
    [drive] = body["drives"]
    [charge] = body["charging"]
    assert drive["km"] == pytest.approx(25.0)
    assert charge["kwh_added"] == pytest.approx(9.1)
    assert charge["location"] == "home"
    for row in (drive, charge):
        assert not {"latitude", "longitude", "lat", "lon", "vin"} & set(row)


@pytest.mark.parametrize("limit", [0, 101])
def test_sessions_limit_is_bounded(client: TestClient, store: Path, limit: int) -> None:
    assert client.get(f"/api/tesla/sessions?limit={limit}").status_code == 422
