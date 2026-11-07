"""API smoke for the Tesla read route.

``GET /api/tesla`` serves the local store only: a request never reaches the
Fleet API, and no route under the prefix accepts anything but GET.
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
    assert {r.path for r in routes} == {"/api/tesla"}
    for route in routes:
        assert route.methods <= {"GET", "HEAD"}, f"{route.path} accepts {route.methods}"


def test_post_to_tesla_is_refused(client: TestClient, store: Path) -> None:
    assert client.post("/api/tesla").status_code == 405


def test_unconfigured_reports_hidden_and_opens_no_store(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "tesla_history.sqlite3"
    monkeypatch.setattr(tesla_history, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.devices.tesla_auth.is_configured", lambda _cfg: False)

    body = client.get("/api/tesla").json()
    assert body["configured"] is False
    assert body["sample"] is None
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

