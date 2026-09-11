"""API smoke for the unit (HVAC) endpoints.

``GET /api/units`` flattens fetched devices (with the boost-delta rule
overlay); ``POST /api/units/{id}`` validates + forwards control payloads
(issue #247 clamping/validation). Cloud fetch/control is monkeypatched —
never the live MELCloud Home backend.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from src.melcloud_client import DeviceInfo


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
    # A unit with no explicit connectivity flag defaults to reachable (#520).
    assert units[0]["reachable"] is True


def test_units_route_reports_unreachable_unit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reachable=False`` reaches the PWA so it can dim/disable the card (#520)."""
    fake = DeviceInfo(
        unit_id="unit-offline",
        name="Fixture Offline",
        building="Fixture",
        power=True,
        operation_mode="Cool",
        room_temperature=22.0,
        set_temperature=24.0,
        fan_speed="Auto",
        reachable=False,
    )

    async def fake_fetch_devices() -> List[DeviceInfo]:
        return [fake]

    monkeypatch.setattr(
        "app.webapp.routers.units.fetch_devices", fake_fetch_devices
    )

    resp = client.get("/api/units")
    assert resp.status_code == 200
    assert resp.json()["units"][0]["reachable"] is False


@pytest.mark.parametrize(
    "operation_mode,cool_target,heat_target,expected_delta",
    [
        ("Cool", 27.0, None, -2.0),
        ("Heat", None, 21.0, 2.0),
    ],
)
def test_units_route_reports_signed_boost_delta(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation_mode: str,
    cool_target,
    heat_target,
    expected_delta: float,
) -> None:
    """``boost_delta_c`` is negative while cooling, positive while heating (#575).

    Reuses the same ``boosted_target()`` the automation engine applies, so the
    sign can never drift from what is actually being written to the unit.
    """
    from src.hvac_automation import TempRule

    fake = DeviceInfo(
        unit_id="unit-boost",
        name="Fixture Boost",
        building="Fixture",
        power=True,
        operation_mode=operation_mode,
        room_temperature=22.0,
        set_temperature=24.0,
        fan_speed="Auto",
    )

    async def fake_fetch_devices() -> List[DeviceInfo]:
        return [fake]

    monkeypatch.setattr("app.webapp.routers.units.fetch_devices", fake_fetch_devices)
    monkeypatch.setattr(
        "app.webapp.routers.units.load_rules",
        lambda: {
            "unit-boost": TempRule(
                enabled=True,
                cool_target=cool_target,
                heat_target=heat_target,
                boost_enabled=True,
                boost_offset_c=2.0,
            )
        },
    )
    monkeypatch.setattr("app.webapp.routers.units.automation.get_boost_active", lambda _uid: True)

    resp = client.get("/api/units")
    assert resp.status_code == 200
    rule = resp.json()["units"][0]["temperature_rule"]
    assert rule["boost_active"] is True
    assert rule["boost_delta_c"] == expected_delta


# ── Issue #247: control-unit input validation + temperature clamping ───


def test_control_unit_non_numeric_temperature_returns_422(
    client: TestClient,
) -> None:
    """A non-numeric ``set_temperature`` returns 422, not 502."""
    resp = client.post("/api/units/unit-x", json={"set_temperature": "hot"})
    assert resp.status_code == 422


def test_control_unit_invalid_operation_mode_returns_422(
    client: TestClient,
) -> None:
    """An unknown ``operation_mode`` string returns 422, not 502."""
    resp = client.post("/api/units/unit-x", json={"operation_mode": "Turbo"})
    assert resp.status_code == 422


def test_control_unit_invalid_fan_speed_returns_422(
    client: TestClient,
) -> None:
    """An unknown ``fan_speed`` string returns 422, not 502."""
    resp = client.post("/api/units/unit-x", json={"fan_speed": "Hurricane"})
    assert resp.status_code == 422


def test_control_unit_invalid_vane_vertical_returns_422(
    client: TestClient,
) -> None:
    """An unknown ``vane_vertical_direction`` string returns 422, not 502."""
    resp = client.post("/api/units/unit-x", json={"vane_vertical_direction": "Bad"})
    assert resp.status_code == 422


def test_control_unit_invalid_vane_horizontal_returns_422(
    client: TestClient,
) -> None:
    """An unknown ``vane_horizontal_direction`` string returns 422, not 502."""
    resp = client.post("/api/units/unit-x", json={"vane_horizontal_direction": "Bad"})
    assert resp.status_code == 422


def test_control_unit_valid_payload_calls_set_device_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid ``POST /api/units/{id}`` reaches ``set_device_state`` and returns 200."""
    calls: dict = {}
    fake_updated = DeviceInfo(
        unit_id="unit-x",
        name="Office",
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

    async def fake_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        calls["unit_id"] = unit_id
        calls["kwargs"] = kwargs
        return fake_updated

    monkeypatch.setattr(
        "app.webapp.routers.units.set_device_state", fake_set_device_state
    )

    resp = client.post(
        "/api/units/unit-x",
        json={"power": True, "operation_mode": "Cool", "set_temperature": 24.0},
    )
    assert resp.status_code == 200
    assert calls["unit_id"] == "unit-x"
    assert calls["kwargs"] == {
        "power": True,
        "operation_mode": "Cool",
        "set_temperature": 24.0,
    }
    # Only the three sent fields are forwarded — omitted fields stay absent.
    assert "fan_speed" not in calls["kwargs"]


def test_control_unit_omitted_fields_not_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields absent from the request body are not forwarded to ``set_device_state``."""
    calls: dict = {}
    fake_updated = DeviceInfo(
        unit_id="unit-x",
        name="Office",
        building="Fixture",
        power=False,
        operation_mode="Heat",
        room_temperature=20.0,
        set_temperature=21.0,
        fan_speed="Auto",
    )

    async def fake_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        calls["kwargs"] = kwargs
        return fake_updated

    monkeypatch.setattr(
        "app.webapp.routers.units.set_device_state", fake_set_device_state
    )

    resp = client.post("/api/units/unit-x", json={"power": False})
    assert resp.status_code == 200
    # Only power was sent; no temperature / mode / fan / vane forwarded.
    assert calls["kwargs"] == {"power": False}


def test_control_unit_power_false_is_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``power: false`` (a falsy value) is forwarded, not treated as absent."""
    calls: dict = {}
    fake_updated = DeviceInfo(
        unit_id="unit-x",
        name="Office",
        building="Fixture",
        power=False,
        operation_mode=None,
        room_temperature=21.0,
        set_temperature=None,
        fan_speed=None,
    )

    async def fake_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        calls["kwargs"] = kwargs
        return fake_updated

    monkeypatch.setattr(
        "app.webapp.routers.units.set_device_state", fake_set_device_state
    )

    resp = client.post("/api/units/unit-x", json={"power": False})
    assert resp.status_code == 200
    assert calls["kwargs"].get("power") is False
