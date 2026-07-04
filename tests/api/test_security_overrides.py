"""API smoke for the alarm-override "bypass after N repeats" rules (issue #341).

``GET/PUT /api/security/overrides`` round-trips the entry list and reports how
many are active. The on-disk store is redirected to ``tmp_path`` so no real
``config/security_override.json`` is touched.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_overrides_round_trip(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.security_override as cfg

    monkeypatch.setattr(cfg, "OVERRIDES_PATH", tmp_path / "security_override.json")

    # Empty to start.
    body = client.get("/api/security/overrides").json()
    assert body == {"enabled": False, "count": 0, "entries": []}

    resp = client.put(
        "/api/security/overrides",
        json={
            "entries": [
                {"id": "puerta-jardin", "zone_id": 12, "max_retries": 2},
                {"id": "ext-cocina", "zone_id": 21, "max_retries": 1, "enabled": False},
                # No numeric zone_id -> backend drops it.
                {"id": "bad", "max_retries": 1},
            ]
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["count"] == 1  # only the enabled entry counts as active
    assert [e["id"] for e in out["entries"]] == ["puerta-jardin", "ext-cocina"]

    reread = client.get("/api/security/overrides").json()
    assert reread["entries"][0]["max_retries"] == 2
    assert reread["entries"][1]["enabled"] is False


def test_overrides_rejects_non_list(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.security_override as cfg

    monkeypatch.setattr(cfg, "OVERRIDES_PATH", tmp_path / "p.json")
    assert client.put("/api/security/overrides", json={"entries": "nope"}).status_code == 400
