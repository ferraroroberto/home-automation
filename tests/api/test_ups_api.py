"""API smoke for the local UPS endpoint.

``GET /api/ups`` serialises local UPS telemetry — USB/NUT read faked, no
real hardware I/O.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.ups_client import UpsState


def test_ups_route_runs_with_monkeypatched_local_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/ups`` serialises local UPS telemetry — USB/NUT faked."""
    state = UpsState(
        available=True,
        source="nut",
        name="pc-ups@127.0.0.1",
        model="Smart-UPS_1000",
        manufacturer="American Power Conversion",
        serial="AS2522161146",
        status="online",
        mains_online=True,
        battery_charge_pct=90,
        runtime_seconds=5502,
        battery_voltage_v=27.2,
        alarms=(),
    )

    monkeypatch.setattr("app.webapp.routers.ups.fetch_ups_state", lambda: state)

    resp = client.get("/api/ups")
    assert resp.status_code == 200
    body = resp.json()["ups"]
    assert body["source"] == "nut"
    assert body["model"] == "Smart-UPS_1000"
    assert body["mains_online"] is True
    assert body["battery_charge_pct"] == 90
    assert body["runtime_seconds"] == 5502
