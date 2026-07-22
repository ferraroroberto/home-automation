"""API smoke for the UPS-triggered PC-fleet shutdown routes (#498).

``GET/PUT /api/pc-fleet/prefs`` round-trips against a tmp-redirected
``pc_fleet_prefs.json``. ``GET /api/pc-fleet/machines`` and
``POST /api/pc-fleet/wake/{host_id}`` proxy the local-llm-hub admin API; the
actual network call is monkeypatched at the module's ``_hub_get``/``_hub_post``
wrappers — never a real request.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` — only what the router touches."""

    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_pc_fleet_prefs_defaults(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.pc_fleet_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "pc_fleet.json")

    body = client.get("/api/pc-fleet/prefs").json()
    assert body == {"enabled": True, "threshold_minutes": 15, "excluded": []}


def test_pc_fleet_prefs_put_persists_and_rereads(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.pc_fleet_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "pc_fleet.json")

    resp = client.put(
        "/api/pc-fleet/prefs",
        json={"enabled": False, "threshold_minutes": 30, "excluded": ["fake-host-1"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": False,
        "threshold_minutes": 30,
        "excluded": ["fake-host-1"],
    }

    reread = client.get("/api/pc-fleet/prefs").json()
    assert reread == {
        "enabled": False,
        "threshold_minutes": 30,
        "excluded": ["fake-host-1"],
    }


def test_pc_fleet_prefs_put_clamps_wild_threshold(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.pc_fleet_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "pc_fleet.json")

    resp = client.put("/api/pc-fleet/prefs", json={"threshold_minutes": 99999})
    assert resp.status_code == 200
    assert resp.json()["threshold_minutes"] == 240

    resp = client.put("/api/pc-fleet/prefs", json={"threshold_minutes": -5})
    assert resp.status_code == 200
    assert resp.json()["threshold_minutes"] == 1


def test_pc_fleet_prefs_put_excluded_round_trips(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.pc_fleet_prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "DEFAULT_PATH", tmp_path / "pc_fleet.json")

    resp = client.put(
        "/api/pc-fleet/prefs",
        json={"excluded": ["fake-satellite-a", "fake-satellite-b"]},
    )
    assert resp.status_code == 200
    assert resp.json()["excluded"] == ["fake-satellite-a", "fake-satellite-b"]

    reread = client.get("/api/pc-fleet/prefs").json()
    assert reread["excluded"] == ["fake-satellite-a", "fake-satellite-b"]


def test_pc_fleet_machines_proxies_hub_200(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.pc_fleet as pc_fleet_mod

    fake_payload = {"machines": [{"host_id": "fake-tower", "status": "online"}]}

    async def fake_get(url: str):
        return _FakeResponse(200, fake_payload)

    monkeypatch.setattr(pc_fleet_mod, "_hub_get", fake_get)

    resp = client.get("/api/pc-fleet/machines")
    assert resp.status_code == 200
    assert resp.json() == fake_payload


def test_pc_fleet_machines_hub_connect_error_is_clean_502(
    client: TestClient, monkeypatch
) -> None:
    import app.webapp.routers.pc_fleet as pc_fleet_mod

    async def fake_get(url: str):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(pc_fleet_mod, "_hub_get", fake_get)

    resp = client.get("/api/pc-fleet/machines")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "hub unreachable"}


def test_pc_fleet_wake_proxies_hub_200(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.pc_fleet as pc_fleet_mod

    fake_payload = {"ok": True, "sent": True}

    async def fake_post(url: str):
        return _FakeResponse(200, fake_payload)

    monkeypatch.setattr(pc_fleet_mod, "_hub_post", fake_post)

    resp = client.post("/api/pc-fleet/wake/fake-tower")
    assert resp.status_code == 200
    assert resp.json() == fake_payload


def test_pc_fleet_wake_passes_through_hub_400(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.pc_fleet as pc_fleet_mod

    async def fake_post(url: str):
        return _FakeResponse(400, {"detail": "no MAC on file"})

    monkeypatch.setattr(pc_fleet_mod, "_hub_post", fake_post)

    resp = client.post("/api/pc-fleet/wake/fake-no-mac")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "no MAC on file"}


def test_pc_fleet_wake_hub_timeout_is_clean_502(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.pc_fleet as pc_fleet_mod

    async def fake_post(url: str):
        raise httpx.TimeoutException("timed out", request=None)

    monkeypatch.setattr(pc_fleet_mod, "_hub_post", fake_post)

    resp = client.post("/api/pc-fleet/wake/fake-tower")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "hub unreachable"}
