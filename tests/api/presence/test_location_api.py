"""API smoke for the home-location endpoint.

``PUT /api/location`` persists the presence "home" reference point.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.location_config import LocationConfig


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
