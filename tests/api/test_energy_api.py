"""API smoke for the energy-flow endpoint.

``GET /api/energy`` flattens an ``EnergyState`` — fetcher faked, never the
live FusionSolar/Modbus backend.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.huawei_client import EnergyState


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
