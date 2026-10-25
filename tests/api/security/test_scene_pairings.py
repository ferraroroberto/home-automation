"""API smoke for the alarm-scene detector→camera pairings (issue #162).

``GET/PUT /api/security/scene-pairings`` round-trips the pairing list and reports
how many are active. The on-disk store is redirected to ``tmp_path`` so no real
``config/alarm_scene_pairings.json`` is touched.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_scene_pairings_round_trip(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.alarm_scene_config as cfg

    monkeypatch.setattr(cfg, "PAIRINGS_PATH", tmp_path / "alarm_scene_pairings.json")

    # Empty to start.
    body = client.get("/api/security/scene-pairings").json()
    assert body == {"enabled": False, "count": 0, "entries": []}

    resp = client.put(
        "/api/security/scene-pairings",
        json={
            "entries": [
                {"id": "garden", "zone_id": 3, "camera_id": "garden",
                 "preset_token": "1", "preset_name": "Barbecue"},
                {"id": "door-off", "zone_id": 5, "camera_id": "door", "enabled": False},
                # Missing camera_id -> backend drops it.
                {"id": "bad", "zone_id": 9},
            ]
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["count"] == 1  # only the enabled, complete pairing counts as active
    assert [e["id"] for e in out["entries"]] == ["garden", "door-off"]

    reread = client.get("/api/security/scene-pairings").json()
    assert reread["entries"][0]["preset_name"] == "Barbecue"
    assert reread["entries"][1]["enabled"] is False


def test_scene_pairings_rejects_non_list(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.alarm_scene_config as cfg

    monkeypatch.setattr(cfg, "PAIRINGS_PATH", tmp_path / "p.json")
    assert client.put("/api/security/scene-pairings", json={"entries": "nope"}).status_code == 400
