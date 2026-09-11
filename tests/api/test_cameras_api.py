"""API smoke for the camera (ONVIF) endpoints.

``GET /api/cameras`` + PTZ/preset/snapshot/stream-token routes drive the
camera client and the scoped stream-token middleware. ONVIF/LAN is never
touched — the client is faked throughout.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient

from src.camera_client import CameraInfo


def test_cameras_route_runs_with_monkeypatched_onvif(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/cameras`` serialises camera state — ONVIF faked, no live camera."""
    cam = CameraInfo(
        id="garden",
        host="192.0.2.20",
        reachable=True,
        manufacturer="REOLINK",
        model="E1 Outdoor Pro",
        firmware="v3.1.0",
        ptz_capable=True,
    )

    async def fake_fetch_cameras() -> List[CameraInfo]:
        return [cam]

    monkeypatch.setattr("app.webapp.routers.cameras.fetch_cameras", fake_fetch_cameras)
    monkeypatch.setattr(
        "app.webapp.routers.cameras.load_camera_display_names",
        lambda: {"garden": "Garden"},
    )
    monkeypatch.setattr("app.webapp.routers.cameras.is_recording", lambda _id: False)

    resp = client.get("/api/cameras")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cameras"][0]["id"] == "garden"
    assert body["cameras"][0]["display_name"] == "Garden"
    assert body["cameras"][0]["model"] == "E1 Outdoor Pro"
    assert body["cameras"][0]["ptz_capable"] is True
    assert body["cameras"][0]["recording"] is False


def test_camera_ptz_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/cameras/{id}/ptz`` start/stop drive the client without a camera."""
    calls: List[str] = []

    async def fake_start(camera_id: str, **_kw) -> None:
        calls.append("start:" + camera_id)

    async def fake_stop(camera_id: str, **_kw) -> None:
        calls.append("stop:" + camera_id)

    monkeypatch.setattr("app.webapp.routers.cameras.ptz_start", fake_start)
    monkeypatch.setattr("app.webapp.routers.cameras.ptz_stop", fake_stop)

    started = client.post("/api/cameras/garden/ptz", json={"action": "start", "direction": "left"})
    stopped = client.post("/api/cameras/garden/ptz", json={"action": "stop"})
    assert started.status_code == 200 and stopped.status_code == 200
    assert calls == ["start:garden", "stop:garden"]


def test_camera_display_name_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PUT /api/cameras/{id}/display_name`` persists via the rename store."""
    saved: dict = {}
    monkeypatch.setattr(
        "app.webapp.routers.cameras.set_camera_display_name",
        lambda cid, name: saved.update({cid: name}),
    )
    resp = client.put("/api/cameras/garden/display_name", json={"display_name": "Patio"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Patio"
    assert saved == {"garden": "Patio"}


def test_camera_stream_token_endpoint_no_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/cameras/stream-token`` returns an empty token when auth is off."""
    from app.webapp.server import app as _app

    monkeypatch.setattr(_app.state.webapp_config, "auth_token", "")
    resp = client.post("/api/cameras/stream-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "" and body["expires_in"] == 0


def test_camera_stream_token_endpoint_issues_verifiable_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auth configured the endpoint returns a token that verifies correctly."""
    from app.webapp.server import app as _app
    from src.camera_token import verify as verify_camera_token

    monkeypatch.setattr(_app.state.webapp_config, "auth_token", "test-bearer-xyz")

    resp = client.post("/api/cameras/stream-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 60
    assert verify_camera_token(body["token"], "test-bearer-xyz") is True
    assert verify_camera_token(body["token"], "wrong-bearer") is False


def test_camera_stream_token_accepted_by_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Middleware accepts ``?camera_token=`` on stream/snapshot paths only.

    Uses a non-loopback client so the bearer gate is active.
    """
    from fastapi.testclient import TestClient as TC

    from app.webapp.server import app as _app
    from src.camera_token import issue as issue_camera_token

    monkeypatch.setattr(_app.state.webapp_config, "auth_token", "test-bearer-xyz")
    monkeypatch.setattr(
        "app.webapp.routers.cameras.read_last_snapshot",
        lambda _: None,
    )

    remote = TC(_app, client=("192.0.2.1", 12345))

    # No credentials → 401.
    assert remote.get("/api/cameras/garden/last_snapshot").status_code == 401

    # Long-lived bearer via header still accepted (existing path unchanged).
    assert remote.get(
        "/api/cameras/garden/last_snapshot",
        headers={"Authorization": "Bearer test-bearer-xyz"},
    ).status_code == 404  # auth passed, no persisted frame → 404

    # Valid scoped camera_token → auth passes (middleware grants access).
    scoped = issue_camera_token("test-bearer-xyz")
    assert remote.get(
        "/api/cameras/garden/last_snapshot",
        params={"camera_token": scoped["token"]},
    ).status_code == 404  # auth passed, no persisted frame → 404

    # Invalid / garbage camera_token → 401.
    assert remote.get(
        "/api/cameras/garden/last_snapshot",
        params={"camera_token": "garbage.value"},
    ).status_code == 401

    # Scoped token must NOT grant access to non-stream paths (e.g. list cameras).
    assert remote.get(
        "/api/cameras",
        params={"camera_token": scoped["token"]},
    ).status_code == 401


def test_camera_last_snapshot_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET …/last_snapshot`` serves the persisted frame, 404 when there's none."""
    monkeypatch.setattr(
        "app.webapp.routers.cameras.read_last_snapshot",
        lambda cid: b"\xff\xd8jpeg" if cid == "garden" else None,
    )
    hit = client.get("/api/cameras/garden/last_snapshot")
    assert hit.status_code == 200
    assert hit.headers["content-type"] == "image/jpeg"
    assert hit.content == b"\xff\xd8jpeg"
    miss = client.get("/api/cameras/attic/last_snapshot")
    assert miss.status_code == 404


def test_camera_ptz_step_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST …/ptz`` with action 'step' drives a single fixed nudge."""
    calls: List[dict] = []

    async def fake_step(camera_id: str, **kw) -> None:
        calls.append({"id": camera_id, **kw})

    monkeypatch.setattr("app.webapp.routers.cameras.ptz_step", fake_step)
    resp = client.post("/api/cameras/garden/ptz", json={"action": "step", "direction": "up"})
    assert resp.status_code == 200
    assert calls and calls[0]["id"] == "garden"
    # 'up' maps to a positive tilt at the gentler step speed, no pan.
    assert calls[0]["pan"] == 0.0 and calls[0]["tilt"] > 0.0


def test_camera_ptz_status_and_absolute_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET …/ptz/status`` reports coordinates; ``POST …/ptz/absolute`` moves."""
    moves: List[dict] = []

    async def fake_status(camera_id: str) -> dict:
        return {"pan": 0.1, "tilt": -0.2, "zoom": 0.0, "absolute": True,
                "pan_range": [-1.0, 1.0], "tilt_range": [-1.0, 1.0], "zoom_range": [0.0, 1.0]}

    async def fake_absolute(camera_id: str, **kw) -> None:
        moves.append({"id": camera_id, **kw})

    monkeypatch.setattr("app.webapp.routers.cameras.get_ptz_status", fake_status)
    monkeypatch.setattr("app.webapp.routers.cameras.ptz_absolute", fake_absolute)

    status = client.get("/api/cameras/garden/ptz/status")
    assert status.status_code == 200 and status.json()["pan"] == 0.1
    moved = client.post(
        "/api/cameras/garden/ptz/absolute", json={"pan": 0.5, "tilt": -0.3, "zoom": 0.2}
    )
    assert moved.status_code == 200
    assert moves == [{"id": "garden", "pan": 0.5, "tilt": -0.3, "zoom": 0.2}]


def test_camera_preset_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presets list / save / goto / delete drive the client without a camera."""
    store: List[dict] = [{"token": "1", "name": "Position 1"}]
    events: List[str] = []

    async def fake_list(camera_id: str) -> List[dict]:
        return store

    async def fake_set(camera_id: str, name: str) -> dict:
        events.append("set:" + name)
        return {"token": "2", "name": name}

    async def fake_goto(camera_id: str, token: str) -> None:
        events.append("goto:" + token)

    async def fake_remove(camera_id: str, token: str) -> None:
        events.append("remove:" + token)

    monkeypatch.setattr("app.webapp.routers.cameras.list_presets", fake_list)
    monkeypatch.setattr("app.webapp.routers.cameras.set_preset", fake_set)
    monkeypatch.setattr("app.webapp.routers.cameras.goto_preset", fake_goto)
    monkeypatch.setattr("app.webapp.routers.cameras.remove_preset", fake_remove)

    listed = client.get("/api/cameras/garden/presets")
    assert listed.status_code == 200 and listed.json()["presets"] == store
    saved = client.post("/api/cameras/garden/presets", json={"name": "Position 2"})
    assert saved.status_code == 200 and saved.json()["token"] == "2"
    gone = client.post("/api/cameras/garden/presets/1/goto")
    removed = client.delete("/api/cameras/garden/presets/1")
    assert gone.status_code == 200 and removed.status_code == 200
    assert events == ["set:Position 2", "goto:1", "remove:1"]
