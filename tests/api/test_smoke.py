"""Python-level API smoke against the real FastAPI app.

Asserts the app imports + wires up and that the credential-free / static
endpoints answer. Cloud-backed routes (``/api/units``, ``/api/energy``) are
exercised with their core fetcher monkeypatched — never the live
MELCloud Home / SMA backends.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceDiagnosticsCache
from src.location_config import LocationConfig
from src.melcloud_client import DeviceInfo
from src.presence_client import PresenceAuthError, PresenceConfig, PresenceEntity
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
    # ac_lost is serialised alongside battery_low — it drives the AC-power-lost
    # badge on the alarm-state line (issue #99), mirroring the battery badge.
    assert body["ac_lost"] is False
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


def test_presence_route_serializes_find_my_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``GET /api/presence`` returns cached diagnostics without real iCloud I/O."""
    entities = [
        PresenceEntity(
            entity_id="home-phone",
            name="Home Phone",
            model="iPhone",
            device_class="iPhone",
            latitude=0.0,
            longitude=0.0,
            horizontal_accuracy_m=8.0,
            last_seen=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
            battery_level_pct=80,
            battery_status="Charging",
            distance_from_home_m=50.0,
            at_home=True,
        ),
        PresenceEntity(
            entity_id="away-phone",
            name="Away Phone",
            model="iPhone",
            device_class="iPhone",
            latitude=0.1,
            longitude=0.0,
            horizontal_accuracy_m=12.0,
            last_seen=None,
            battery_level_pct=None,
            battery_status=None,
            distance_from_home_m=1000.0,
            at_home=False,
        ),
        PresenceEntity(
            entity_id="tag",
            name="Keys",
            model="AirTag",
            device_class="Accessory",
            latitude=None,
            longitude=None,
            horizontal_accuracy_m=None,
            last_seen=None,
            battery_level_pct=None,
            battery_status=None,
        ),
    ]

    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=entities,
            refreshed_at=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            available=True,
            reason="ok",
            home_radius_m=200,
        ),
    )

    resp = client.get("/api/presence")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["total_count"] == 3
    assert body["located_count"] == 2
    assert body["home_count"] == 1
    assert body["away_count"] == 1
    assert body["unknown_count"] == 1
    assert body["all_away"] is False
    assert body["home_radius_m"] == 200
    assert body["entities"][0]["last_seen"] == "2026-06-22T10:00:00+00:00"
    assert body["diagnostics"]["available"] is True


def test_presence_route_returns_unavailable_when_icloud_needs_2fa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            available=False,
            reason="2fa_required",
            detail="iCloud requires 2FA",
        ),
    )

    resp = client.get("/api/presence")
    assert resp.status_code == 200
    assert resp.json()["diagnostics"]["reason"] == "2fa_required"


def test_presence_hidden_and_display_name_persist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_display_names as pdn
    import src.presence_hidden as ph

    names = tmp_path / "presence_display_names.json"
    hidden = tmp_path / "presence_hidden.json"
    monkeypatch.setattr(pdn, "DEFAULT_PATH", names)
    monkeypatch.setattr(ph, "DEFAULT_PATH", hidden)

    resp = client.put(
        "/api/presence/entity-display-name",
        json={"entity_id": "ana", "display_name": "Ana"},
    )
    assert resp.status_code == 200
    assert pdn.load_presence_display_names() == {"ana": "Ana"}

    resp = client.put(
        "/api/presence/entity-hidden",
        json={"entity_id": "ana", "hidden": True},
    )
    assert resp.status_code == 200
    assert ph.load_hidden_presence_ids() == {"ana"}

    unsafe_id = "Find/My+Accessory/2"
    resp = client.put(
        "/api/presence/entity-hidden",
        json={"entity_id": unsafe_id, "hidden": True},
    )
    assert resp.status_code == 200
    assert unsafe_id in ph.load_hidden_presence_ids()


def test_location_endpoint_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = tmp_path / "location.json"
    monkeypatch.setattr(
        "app.webapp.routers.presence.load_location_config",
        lambda: LocationConfig(1.0, 2.0, "Old") if store.exists() else None,
    )
    saved = {}

    def fake_save(location: LocationConfig) -> None:
        saved["location"] = location
        store.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("app.webapp.routers.presence.save_location_config", fake_save)
    resp = client.put("/api/location", json={"lat": 41.1, "lon": 2.1, "label": "Home"})
    assert resp.status_code == 200
    assert saved["location"] == LocationConfig(41.1, 2.1, "Home")


def test_push_subscription_endpoint_accepts_first_subscription(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.push_notifications as push

    store = tmp_path / "push_subscriptions.json"
    monkeypatch.setattr(push, "SUBSCRIPTIONS_PATH", store)

    resp = client.post(
        "/api/push/subscriptions",
        json={
            "endpoint": "https://push.example/sub",
            "keys": {"p256dh": "fixture", "auth": "secret"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 1}
    assert push.load_subscriptions() == [
        {
            "endpoint": "https://push.example/sub",
            "keys": {"p256dh": "fixture", "auth": "secret"},
        }
    ]
