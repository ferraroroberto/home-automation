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
from src.risco_client import SecurityState, SecurityZone
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


def test_security_route_surfaces_battery_and_trouble(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/security`` serialises the system battery flag + per-zone trouble.

    The cloud exposes no per-detector battery, so issue #84 surfaces the
    system-wide ``battery_low`` flag (alert source) plus the per-zone generic
    ``trouble`` flag. Cloud fetch is faked — no RISCO login.
    """
    state = SecurityState(
        reachable=True,
        label="Disarmed",
        mode="disarmed",
        zones=[
            SecurityZone(id=0, name="1", type=1, trouble=True),
            SecurityZone(id=4, name="Garage", type=2, trouble=False),
        ],
        battery_low=True,
        ac_lost=False,
    )

    async def fake_fetch_security_state() -> SecurityState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.security.fetch_security_state", fake_fetch_security_state
    )

    resp = client.get("/api/security")
    assert resp.status_code == 200
    body = resp.json()
    assert body["battery_low"] is True
    zones = body["zones"]
    assert zones[0]["trouble"] is True
    assert zones[1]["trouble"] is False
    # Display-name override is merged per zone (None when unset).
    assert "display_name" in zones[0]


def test_security_zone_rename_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``PUT /api/security/zones/{id}/display_name`` writes the override atomically."""
    import src.security_display_names as sdn

    store = tmp_path / "security_display_names.json"
    monkeypatch.setattr(sdn, "DEFAULT_PATH", store)

    resp = client.put("/api/security/zones/4/display_name", json={"display_name": "Garage"})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "display_name": "Garage"}
    assert sdn.load_security_display_names() == {"4": "Garage"}

    # Clearing removes the entry.
    resp = client.put("/api/security/zones/4/display_name", json={"display_name": "  "})
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None
    assert sdn.load_security_display_names() == {}


def test_security_zone_hidden_persists_and_merges(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``PUT /api/security/zones/{id}/hidden`` persists, and GET merges the flag."""
    import src.security_hidden as shd

    store = tmp_path / "security_hidden.json"
    monkeypatch.setattr(shd, "DEFAULT_PATH", store)

    # Hide zone 4, then verify it round-trips on disk.
    resp = client.put("/api/security/zones/4/hidden", json={"hidden": True})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "hidden": True}
    assert shd.load_hidden_zone_ids() == {"4"}

    # GET merges the hidden flag per zone (only the hidden one is True).
    state = SecurityState(
        reachable=True,
        label="Disarmed",
        mode="disarmed",
        zones=[
            SecurityZone(id=0, name="1", type=1),
            SecurityZone(id=4, name="Garage", type=2),
        ],
    )

    async def fake_fetch_security_state() -> SecurityState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.security.fetch_security_state", fake_fetch_security_state
    )
    body = client.get("/api/security").json()
    zones = {z["id"]: z["hidden"] for z in body["zones"]}
    assert zones == {0: False, 4: True}

    # Un-hiding clears the entry.
    resp = client.put("/api/security/zones/4/hidden", json={"hidden": False})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "hidden": False}
    assert shd.load_hidden_zone_ids() == set()
