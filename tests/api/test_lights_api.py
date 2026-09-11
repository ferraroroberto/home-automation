"""API smoke for the Elgato light endpoints.

``GET /api/lights`` serialises Elgato state (incl. the MAC-keyed
display-name migration); ``POST/PUT`` drive control + rename. LAN is never
touched — the client is faked throughout.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from src.elgato_client import ElgatoLight


def test_lights_route_runs_with_monkeypatched_lan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/lights`` serialises Elgato state — fetcher faked, no LAN."""
    light = ElgatoLight(
        light_id="192.0.2.10:9123",
        host="192.0.2.10",
        port=9123,
        name="Fixture Key Light",
        product_name="Elgato Key Light",
        firmware="1.0",
        on=True,
        brightness=42,
        temperature=200,
        temperature_k=5000,
        supports_temperature=True,
        mac_address="AA:BB:CC:DD:EE:FF",
    )

    async def fake_fetch_lights() -> List[ElgatoLight]:
        return [light]

    monkeypatch.setattr("app.webapp.routers.lights.fetch_lights", fake_fetch_lights)
    monkeypatch.setattr(
        "app.webapp.routers.lights.load_elgato_display_names",
        lambda: {"mac:AA:BB:CC:DD:EE:FF": "Desk left"},
    )

    resp = client.get("/api/lights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lights"][0]["light_id"] == "192.0.2.10:9123"
    assert body["lights"][0]["display_key"] == "mac:AA:BB:CC:DD:EE:FF"
    assert body["lights"][0]["display_name"] == "Desk left"
    assert body["lights"][0]["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert body["lights"][0]["brightness"] == 42
    assert body["lights"][0]["temperature_k"] == 5000
    assert body["lights"][0]["supports_temperature"] is True


def test_lights_route_keeps_legacy_host_display_name_fallback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing host-keyed labels still apply until a MAC-keyed save migrates them."""
    light = ElgatoLight(
        light_id="192.0.2.30:9123",
        host="192.0.2.30",
        port=9123,
        name="Fixture Key Light",
        product_name="Elgato Key Light",
        firmware="1.0",
        on=True,
        brightness=42,
        temperature=200,
        temperature_k=5000,
        supports_temperature=True,
        mac_address="AA:BB:CC:DD:EE:FF",
    )

    async def fake_fetch_lights() -> List[ElgatoLight]:
        return [light]

    monkeypatch.setattr("app.webapp.routers.lights.fetch_lights", fake_fetch_lights)
    monkeypatch.setattr(
        "app.webapp.routers.lights.load_elgato_display_names",
        lambda: {"192.0.2.30:9123": "Legacy desk"},
    )

    body = client.get("/api/lights").json()
    assert body["lights"][0]["display_key"] == "mac:AA:BB:CC:DD:EE:FF"
    assert body["lights"][0]["display_name"] == "Legacy desk"


def test_lights_control_route_reads_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/lights/{id}`` returns the accepted read-back state."""
    calls = {}
    accepted = ElgatoLight(
        light_id="192.0.2.10:9123",
        host="192.0.2.10",
        port=9123,
        name="Fixture Key Light",
        product_name="Elgato Key Light",
        firmware="1.0",
        on=False,
        brightness=30,
        temperature=250,
        temperature_k=4000,
        supports_temperature=True,
    )

    async def fake_set_light_state(light_id: str, **kwargs) -> ElgatoLight:
        calls["light_id"] = light_id
        calls["kwargs"] = kwargs
        return accepted

    monkeypatch.setattr(
        "app.webapp.routers.lights.set_light_state", fake_set_light_state
    )

    resp = client.post(
        "/api/lights/192.0.2.10:9123",
        json={"on": False, "brightness": 30, "temperature_k": 4000},
    )
    assert resp.status_code == 200
    assert calls == {
        "light_id": "192.0.2.10:9123",
        "kwargs": {
            "on": False,
            "brightness": 30,
            "temperature": None,
            "temperature_k": 4000,
        },
    }
    assert resp.json()["on"] is False


def test_lights_display_name_route_persists_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PUT /api/lights/{id}/display_name`` stores the local label override."""
    calls = {}

    def fake_set_display_name(light_id: str, display_name: str) -> None:
        calls["light_id"] = light_id
        calls["display_name"] = display_name

    monkeypatch.setattr(
        "app.webapp.routers.lights.set_elgato_display_name", fake_set_display_name
    )

    resp = client.put(
        "/api/lights/192.0.2.10:9123/display_name",
        json={"display_name": " Desk left ", "display_key": "mac:AA:BB:CC:DD:EE:FF"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "light_id": "192.0.2.10:9123",
        "display_key": "mac:AA:BB:CC:DD:EE:FF",
        "display_name": "Desk left",
    }
    assert calls == {"light_id": "mac:AA:BB:CC:DD:EE:FF", "display_name": "Desk left"}
