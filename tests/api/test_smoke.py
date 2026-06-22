"""Python-level API smoke against the real FastAPI app.

Asserts the app imports + wires up and that the credential-free / static
endpoints answer. Cloud-backed routes (``/api/units``, ``/api/energy``) are
exercised with their core fetcher monkeypatched — never the live
MELCloud Home / SMA backends.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from src.melcloud_client import DeviceInfo
from src.sma_client import EnergyState


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_version_shape(client: TestClient) -> None:
    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    # Build identity the PWA footer + restart-recipe check rely on.
    assert "git_sha" in body and "built_at" in body
    assert isinstance(body["git_sha"], str) and body["git_sha"]


def test_index_serves_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_units_route_runs_with_monkeypatched_cloud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/units`` flattens the fetched devices — fetcher faked, no cloud."""
    fake = DeviceInfo(
        unit_id="unit-x",
        name="Fixture Office",
        building="Fixture",
        power=True,
        operation_mode="Cool",
        room_temperature=22.0,
        set_temperature=24.0,
        fan_speed="Auto",
        operation_modes=["Heat", "Cool"],
        fan_speeds=["Auto", "One"],
        temp_ranges={"Cool": (16.0, 31.0)},
    )

    async def fake_fetch_devices() -> List[DeviceInfo]:
        return [fake]

    monkeypatch.setattr(
        "app.webapp.routers.units.fetch_devices", fake_fetch_devices
    )

    resp = client.get("/api/units")
    assert resp.status_code == 200
    units = resp.json()["units"]
    assert len(units) == 1
    assert units[0]["unit_id"] == "unit-x"
    assert units[0]["operation_mode"] == "Cool"
    # temp_ranges tuples are serialised to lists for JSON.
    assert units[0]["temp_ranges"]["Cool"] == [16.0, 31.0]


def test_energy_route_runs_with_monkeypatched_cloud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/energy`` flattens an ``EnergyState`` — fetcher faked, no cloud."""
    state = EnergyState(
        grid_import_w=0.0,
        grid_export_w=1200.0,
        pv_power_w=2500.0,
        house_consumption_w=1300.0,
        pv_surplus_w=1200.0,
        meter_reachable=True,
        inverter_reachable=True,
    )

    async def fake_fetch_energy_state() -> EnergyState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.energy.fetch_energy_state", fake_fetch_energy_state
    )

    resp = client.get("/api/energy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pv_power_w"] == 2500.0
    assert body["inverter_reachable"] is True
    assert body["meter_reachable"] is True
