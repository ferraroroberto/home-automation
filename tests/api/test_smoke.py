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
from src.camera_client import CameraInfo
from src.elgato_client import ElgatoLight
from src.location_config import LocationConfig
from src.melcloud_client import DeviceInfo
from src.network_client import (
    AccessPointHealth,
    InternetHealth,
    NetDevice,
    NetworkState,
    RouterHealth,
    WifiBssid,
    WifiChannelInsight,
    WifiChannelScore,
    WifiDiagnostics,
)
from src.presence_client import PresenceAuthError, PresenceConfig, PresenceEntity
from src.risco_client import SecurityState, SecurityZone
from src.sma_client import EnergyState
from src.ups_client import UpsState


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


def test_hyperv_route_runs_with_monkeypatched_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/hyperv`` serialises the VM state — Hyper-V faked, no host I/O."""
    from src.hyperv_client import HyperVState

    state = HyperVState(
        available=True,
        name="Home Assistant",
        state="running",
        uptime_seconds=274800,
        ip_address="192.168.0.4",
        mac_address="00:15:5D:01:2A:0B",
    )
    monkeypatch.setattr("app.webapp.routers.hyperv.fetch_hyperv_state", lambda: state)

    resp = client.get("/api/hyperv")
    assert resp.status_code == 200
    body = resp.json()["hyperv"]
    assert body["state"] == "running"
    assert body["ip_address"] == "192.168.0.4"
    assert body["mac_address"] == "00:15:5D:01:2A:0B"
    assert body["uptime_seconds"] == 274800


def test_hyperv_missing_config_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``HA_VM_NAME`` surfaces as 503, not a 500."""
    from src.hyperv_client import HyperVConfigError

    def boom() -> None:
        raise HyperVConfigError("HA_VM_NAME is not set — add the Hyper-V VM name to .env.")

    monkeypatch.setattr("app.webapp.routers.hyperv.fetch_hyperv_state", boom)
    assert client.get("/api/hyperv").status_code == 503


def test_hyperv_start_stop_invoke_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/hyperv/{start|stop}`` calls the core and returns read-back state."""
    import app.webapp.routers.hyperv as hv
    from src.hyperv_client import HyperVState

    calls: List[str] = []

    def fake_start() -> HyperVState:
        calls.append("start")
        return HyperVState(available=True, name="HA", state="running", uptime_seconds=1)

    def fake_stop() -> HyperVState:
        calls.append("stop")
        return HyperVState(available=True, name="HA", state="off", uptime_seconds=0)

    # The route resolves the action via a dict built at import time, so patch it.
    monkeypatch.setitem(hv._ACTIONS, "start", fake_start)
    monkeypatch.setitem(hv._ACTIONS, "stop", fake_stop)

    started = client.post("/api/hyperv/start")
    stopped = client.post("/api/hyperv/stop")
    assert started.status_code == 200 and started.json()["hyperv"]["state"] == "running"
    assert stopped.status_code == 200 and stopped.json()["hyperv"]["state"] == "off"
    assert calls == ["start", "stop"]


def test_hyperv_action_error_mapping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each distinct cause maps to its own status: 409/404/403, and 400 for a bad action."""
    import app.webapp.routers.hyperv as hv
    from src.hyperv_client import (
        HyperVNotFoundError,
        HyperVPermissionError,
        HyperVStateError,
    )

    def already() -> None:
        raise HyperVStateError("VM 'HA' is already running.")

    def missing() -> None:
        raise HyperVNotFoundError("VM 'HA' was not found on this host.")

    def denied() -> None:
        raise HyperVPermissionError("Insufficient Hyper-V rights.")

    monkeypatch.setitem(hv._ACTIONS, "start", already)
    assert client.post("/api/hyperv/start").status_code == 409

    monkeypatch.setitem(hv._ACTIONS, "start", missing)
    assert client.post("/api/hyperv/start").status_code == 404

    monkeypatch.setitem(hv._ACTIONS, "start", denied)
    assert client.post("/api/hyperv/start").status_code == 403

    # An unknown action never reaches the core.
    assert client.post("/api/hyperv/restart").status_code == 400


def test_network_visibility_and_wifi_rename_persist_and_merge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Network hidden/name stores persist, and ``GET /api/network`` merges them."""
    import src.network_hidden as nh
    import src.network_wifi_display_names as wdn

    monkeypatch.setattr(nh, "DEVICE_DEFAULT_PATH", tmp_path / "network_hidden.json")
    monkeypatch.setattr(nh, "WIFI_DEFAULT_PATH", tmp_path / "network_wifi_hidden.json")
    monkeypatch.setattr(wdn, "DEFAULT_PATH", tmp_path / "network_wifi_display_names.json")
    monkeypatch.setattr(
        "app.webapp.routers.network.record_and_snapshot",
        lambda _seen, _now: ([], {}),
    )

    network_state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=1),
        router=RouterHealth(reachable=True, authenticated=True),
        devices=(
            NetDevice(
                mac="AA:00:00:00:00:01",
                ip="192.0.2.11",
                name="Fixture Phone",
                conn_type="5GHz",
                signal=70,
                link_rate=300,
                ssid="Home",
                source="ap",
            ),
        ),
        wifi=WifiDiagnostics(
            available=True,
            current_ssid="Home",
            current_bssid="AA:BB:CC:DD:EE:01",
            bssids=(
                WifiBssid(
                    ssid="Home",
                    bssid="AA:BB:CC:DD:EE:01",
                    signal=86,
                    rssi_dbm=-57,
                    channel=44,
                    band="5GHz",
                    radio_type="802.11ac",
                    authentication="WPA2-Personal",
                    encryption="CCMP",
                    connected=True,
                ),
            ),
        ),
        alerts=(),
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return network_state

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )

    resp = client.put("/api/network/devices/AA:00:00:00:00:01/hidden", json={"hidden": True})
    assert resp.status_code == 200
    assert resp.json() == {"mac": "AA:00:00:00:00:01", "hidden": True}

    resp = client.put(
        "/api/network/wifi/display_name",
        json={"wifi_id": "AA:BB:CC:DD:EE:01", "display_name": "Main AP"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"wifi_id": "AA:BB:CC:DD:EE:01", "display_name": "Main AP"}

    resp = client.put(
        "/api/network/wifi/hidden",
        json={"wifi_id": "AA:BB:CC:DD:EE:01", "hidden": True},
    )
    assert resp.status_code == 200
    assert resp.json() == {"wifi_id": "AA:BB:CC:DD:EE:01", "hidden": True}

    body = client.get("/api/network").json()
    assert body["devices"][0]["hidden"] is True
    wifi = body["wifi"]["bssids"][0]
    assert wifi["wifi_id"] == "AA:BB:CC:DD:EE:01"
    assert wifi["display_name"] == "Main AP"
    assert wifi["original_name"] == "Home"
    assert wifi["hidden"] is True


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


def test_tuya_route_surfaces_no_ip_identity_and_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-IP Tuya rows stay visible with stable non-secret identity metadata."""
    from src.tuya_client import TuyaDeviceInfo

    info = TuyaDeviceInfo(
        device_id="plug-noip",
        name="Fixture Plug",
        category="cz",
        mac="AA:BB:CC:DD:EE:FF",
        uuid="uuid-fixture",
        sn="sn-fixture",
        ip="Auto",
        has_valid_ip=False,
        has_local_key=True,
        switch_dps="1",
    )

    monkeypatch.setattr("app.webapp.routers.tuya.list_devices", lambda: [info])
    monkeypatch.setattr("app.webapp.routers.tuya.load_tuya_display_names", lambda: {})

    body = client.get("/api/tuya").json()
    card = body["devices"][0]
    assert card["device_id"] == "plug-noip"
    assert card["mac"] == "AA:BB:CC:DD:EE:FF"
    assert card["uuid"] == "uuid-fixture"
    assert card["sn"] == "sn-fixture"
    assert card["ip"] == "Auto"
    assert card["reachable"] is False
    # A no-IP device with a key reports the LAN-scan reason, not the wizard one.
    assert "No local IP" in card["error"]

    # Refresh now runs a LAN rescan server-side; fake it so the test never scans.
    monkeypatch.setattr(
        "app.webapp.routers.tuya.rescan_addresses",
        lambda: {"found": 2, "updated": ["plug-noip"], "addresses": {"plug-noip": "192.0.2.7"}},
    )
    refreshed = client.post("/api/tuya/refresh")
    assert refreshed.status_code == 200
    refresh = refreshed.json()["refresh"]
    assert refresh["safe"] is True
    assert refresh["found"] == 2
    assert refresh["updated"] == ["plug-noip"]
    assert "recovered 1 stale" in refresh["detail"]


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


def test_security_route_surfaces_trouble_and_ac_lost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/security`` serialises the AC-power flag + per-zone trouble.

    The cloud exposes no per-detector battery (low-battery was dropped in #227),
    so the surfaced system flags are ``ac_lost`` (issue #99) plus the per-zone
    generic ``trouble`` flag (issue #84). Cloud fetch is faked — no RISCO login.
    """
    state = SecurityState(
        reachable=True,
        label="Disarmed",
        mode="disarmed",
        zones=[
            SecurityZone(id=0, name="1", type=1, trouble=True),
            SecurityZone(id=4, name="Garage", type=2, trouble=False),
        ],
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
    # Low-battery was removed (#227); no battery_* keys should leak through.
    assert "battery_low" not in body
    assert "battery_acknowledged" not in body
    # ac_lost drives the AC-power-lost badge on the alarm-state line (issue #99).
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


def test_security_schedules_endpoint_persists_normalized_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``GET/PUT /api/security/schedules`` edits the local weekly alarm schedule list."""
    import src.security_schedules as schedules

    store = tmp_path / "security_schedules.json"
    monkeypatch.setattr(schedules, "SCHEDULES_PATH", store)

    resp = client.get("/api/security/schedules")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "count": 0, "entries": []}

    resp = client.put(
        "/api/security/schedules",
        json={
            "entries": [
                {
                    "id": "Weekend full",
                    "enabled": True,
                    "time": "22:30",
                    "days": ["sat", "sun"],
                    "action": "arm",
                },
                {
                    "id": "weekday-disarm",
                    "enabled": False,
                    "time": "07:15",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "action": "disarm",
                },
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["entries"][0] == {
        "id": "Weekend-full",
        "enabled": True,
        "time": "22:30",
        "days": ["sat", "sun"],
        "action": "arm",
    }
    assert schedules.load_security_schedules(path=store)[1].enabled is False


def test_network_route_flattens_state_with_monkeypatched_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/network`` flattens a ``NetworkState`` — core faked, no LAN I/O.

    An unreachable router stays 200 (reported on its card, not a 500), and
    ``is_wireless`` is sent per device so the band-grouped list doesn't re-derive
    the wired/wireless split client-side (issue #129). Phase-2 identity is layered
    on at render time: the OUI ``vendor``, a coarse ``category``, the ``randomized``
    flag, and the per-MAC ``display_name`` override.
    """
    state = NetworkState(
        internet=InternetHealth(
            online=True, gateway_ms=3.0, external_ms=12.0, packet_loss_pct=0.0
        ),
        access_point=AccessPointHealth(
            reachable=True, model="R9000", firmware="1.0", mode="access_point", device_count=2
        ),
        router=RouterHealth(reachable=False, error="no response"),
        wifi=WifiDiagnostics(
            available=True,
            interface_name="Wi-Fi",
            adapter_description="Fixture WLAN",
            current_ssid="Home",
            current_bssid="AA:BB:CC:DD:EE:01",
            current_signal=86,
            current_channel=44,
            current_band="5GHz",
            current_radio_type="802.11ac",
            bssids=(
                WifiBssid(
                    ssid="Home", bssid="AA:BB:CC:DD:EE:01", signal=86, rssi_dbm=-57,
                    channel=44, band="5GHz", radio_type="802.11ac",
                    authentication="WPA2-Personal", encryption="CCMP", connected=True,
                ),
                WifiBssid(
                    ssid="Neighbour", bssid="AA:BB:CC:DD:EE:02", signal=42, rssi_dbm=-79,
                    channel=6, band="2.4GHz", radio_type="802.11n",
                    authentication="WPA2-Personal", encryption="CCMP",
                ),
            ),
            recommendations=("Current Wi-Fi signal is strong (86%).",),
            insights=(
                WifiChannelInsight(
                    band="2.4GHz",
                    source="windows_netsh",
                    recommended_channel=1,
                    recommended_width_mhz=20,
                    coordinated_channels=(1, 8),
                    candidate_scores=(
                        WifiChannelScore(
                            channel=1,
                            score=12.5,
                            visible_radios=1,
                            strongest_signal=42,
                            strongest_ssid="Neighbour",
                        ),
                    ),
                    rationale=("Lower score means cleaner.",),
                ),
            ),
        ),
        devices=(
            # Espressif IoT chip with no hostname — vendor + category make it legible.
            NetDevice(
                mac="5C:CF:7F:AA:BB:CC", ip="192.168.0.5", name=None, conn_type="5GHz",
                signal=28, link_rate=300, ssid="Home", source="ap",
            ),
            # Wired host carrying a custom label (override merged below).
            NetDevice(
                mac="B8:27:EB:11:22:33", ip="192.168.0.10", name="nas", conn_type="wired",
                signal=None, link_rate=1000, ssid=None, source="ap",
            ),
        ),
        alerts=("1 wireless client(s) on weak signal (<40%).",),
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        assert include_speedtest is False  # plain poll never runs the speed test
        return state

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )
    # A custom label for the wired host, keyed by the normalised MAC.
    monkeypatch.setattr(
        "app.webapp.routers.network.load_network_display_names",
        lambda: {"B8:27:EB:11:22:33": "Office Pi"},
    )

    resp = client.get("/api/network")
    assert resp.status_code == 200
    body = resp.json()
    assert body["internet"]["online"] is True
    assert body["access_point"]["device_count"] == 2
    assert body["router"]["reachable"] is False
    assert body["wifi"]["available"] is True
    assert body["wifi"]["current_ssid"] == "Home"
    assert body["wifi"]["current_signal"] == 86
    assert body["wifi"]["bssids"][0]["connected"] is True
    assert body["wifi"]["bssids"][1]["band"] == "2.4GHz"
    assert body["wifi"]["insights"][0] == {
        "band": "2.4GHz",
        "source": "windows_netsh",
        "recommended_channel": 1,
        "recommended_width_mhz": 20,
        "coordinated_channels": [1, 8],
        "candidate_scores": [
            {
                "channel": 1,
                "score": 12.5,
                "visible_radios": 1,
                "strongest_signal": 42,
                "strongest_ssid": "Neighbour",
            }
        ],
        "rationale": ["Lower score means cleaner."],
        "apply_supported": False,
    }
    # An unreachable router carries no WAN detail (all Phase-3 fields null).
    assert body["router"]["wan_online"] is None
    assert body["router"]["public_ip"] is None
    assert [d["is_wireless"] for d in body["devices"]] == [True, False]
    assert body["alerts"][0].startswith("1 wireless")

    iot, wired = body["devices"]
    assert iot["vendor"] == "Espressif"
    assert iot["category"] == "iot"
    assert iot["randomized"] is False
    assert iot["display_name"] is None
    assert wired["vendor"] == "Raspberry Pi"
    assert wired["display_name"] == "Office Pi"
    assert wired["category"] == "nas"  # hostname "nas" keyword wins
    # Phase-4 history fields ride along: a live device is online, not important by
    # default, and the cold-start seed read never flags the whole inventory "new".
    assert iot["online"] is True and wired["online"] is True
    assert iot["important"] is False
    assert iot["is_new"] is False


def test_network_route_surfaces_wifi_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wi-Fi diagnostics failure stays local to the ``wifi`` block."""
    state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, model="R9000", device_count=0),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(
            available=False,
            interface_name="Wi-Fi",
            adapter_description="Fixture WLAN",
            error="No Wi-Fi networks visible.",
        ),
        devices=(),
        alerts=(),
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )

    resp = client.get("/api/network")

    assert resp.status_code == 200
    body = resp.json()
    assert body["wifi"]["available"] is False
    assert body["wifi"]["interface_name"] == "Wi-Fi"
    assert body["wifi"]["bssids"] == []
    assert body["wifi"]["error"] == "No Wi-Fi networks visible."
    assert body["devices"] == []


def test_network_reboot_access_point_invokes_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/network/access-point/reboot`` calls the core and returns ok."""
    calls = {"n": 0}

    def fake_reboot() -> None:
        calls["n"] += 1

    monkeypatch.setattr(
        "app.webapp.routers.network.reboot_access_point", fake_reboot
    )

    resp = client.post("/api/network/access-point/reboot")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls["n"] == 1


def test_network_reboot_router_invokes_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/network/router/reboot`` calls the core and returns ok (Phase 3)."""
    calls = {"n": 0}

    def fake_reboot() -> None:
        calls["n"] += 1

    monkeypatch.setattr("app.webapp.routers.network.reboot_router", fake_reboot)
    resp = client.post("/api/network/router/reboot")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls["n"] == 1


def test_router_wan_parse_picks_live_internet_connection() -> None:
    """``_pick_internet_wan`` skips disconnected/0.0.0.0 instances, prefers default-GW."""
    from src.network_client import _parse_instances, _pick_internet_wan

    xml = (
        "<root>"
        "<Instance><ParaName>WANCName</ParaName><ParaValue>FTTH-TV</ParaValue>"
        "<ParaName>ConnStatus</ParaName><ParaValue>Connected</ParaValue>"
        "<ParaName>IPAddress</ParaName><ParaValue>0.0.0.0</ParaValue>"
        "<ParaName>IsDefGW</ParaName><ParaValue>0</ParaValue></Instance>"
        "<Instance><ParaName>WANCName</ParaName><ParaValue>FTTH-Data</ParaValue>"
        "<ParaName>ConnStatus</ParaName><ParaValue>Connected</ParaValue>"
        "<ParaName>IPAddress</ParaName><ParaValue>188.87.72.66</ParaValue>"
        "<ParaName>GateWay</ParaName><ParaValue>87.235.0.10</ParaValue>"
        "<ParaName>IsDefGW</ParaName><ParaValue>1</ParaValue>"
        "<ParaName>UpTime</ParaName><ParaValue>1298319</ParaValue></Instance>"
        "</root>"
    )
    insts = _parse_instances(xml)
    assert len(insts) == 2
    wan = _pick_internet_wan(insts)
    assert wan["WANCName"] == "FTTH-Data"  # TV (0.0.0.0) skipped; default-GW chosen
    assert wan["IPAddress"] == "188.87.72.66"
    # No connected real-IP instance → {} (router up, internet down).
    assert _pick_internet_wan([{"ConnStatus": "Disconnected", "IPAddress": "0.0.0.0"}]) == {}


def test_router_asy_encode_is_rsa_pkcs1v15_base64() -> None:
    """``_asy_encode`` mirrors JSEncrypt: RSA/PKCS1v15 of the digest, base64-encoded."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from src.network_client import _asy_encode

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    encoded = _asy_encode(pem, "deadbeef")
    # The matching private key recovers the source → the encrypt path is correct.
    assert priv.decrypt(base64.b64decode(encoded), padding.PKCS1v15()) == b"deadbeef"


def test_network_device_rename_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``PUT /api/network/devices/{mac}/display_name`` writes the override atomically.

    The MAC is normalised (upper-cased) before it becomes the store key, so a
    rename keyed under any casing round-trips (issue #129 Phase 2).
    """
    import src.network_display_names as ndn

    store = tmp_path / "network_display_names.json"
    monkeypatch.setattr(ndn, "DEFAULT_PATH", store)

    resp = client.put(
        "/api/network/devices/a4:cf:12:aa:bb:cc/display_name",
        json={"display_name": "Garage sensor"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"mac": "A4:CF:12:AA:BB:CC", "display_name": "Garage sensor"}
    assert ndn.load_network_display_names() == {"A4:CF:12:AA:BB:CC": "Garage sensor"}

    # Clearing removes the entry.
    resp = client.put(
        "/api/network/devices/A4:CF:12:AA:BB:CC/display_name",
        json={"display_name": "  "},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None
    assert ndn.load_network_display_names() == {}


def test_network_oui_vendor_and_category_heuristics() -> None:
    """The bundled OUI table + heuristics: known prefix, miss, and randomised MAC."""
    from src.network_oui import (
        category_for_device,
        is_randomized_mac,
        vendor_for_mac,
    )

    # A known prefix resolves regardless of casing/separators; an unknown one is None.
    assert vendor_for_mac("5c:cf:7f:11:22:33") == "Espressif"
    assert vendor_for_mac("B8-27-EB-44-55-66") == "Raspberry Pi"
    assert vendor_for_mac("02:00:00:00:00:01") is None  # unknown + locally-administered

    # Locally-administered (randomised) addresses are flagged and never vendored.
    assert is_randomized_mac("DA:A1:19:00:00:01") is True   # 0xDA bit-1 set
    assert is_randomized_mac("B8:27:EB:00:00:01") is False  # universally administered
    assert vendor_for_mac("DA:A1:19:00:00:01") is None

    # Category: hostname keyword wins; vendor is the weak fallback; else unknown.
    assert category_for_device("Kitchen-iPad", "Apple", "5GHz") == "phone"
    assert category_for_device(None, "Espressif", "5GHz") == "iot"
    assert category_for_device("office-laserjet", None, "wired") == "printer"
    assert category_for_device(None, None, "wired") == "unknown"


def test_network_history_store_seeds_then_flags_new_and_prunes(tmp_path) -> None:
    """The MAC registry: silent cold-start seed, later-arrival ``new``, 180-day prune."""
    from src.network_history import (
        is_new,
        load_network_history,
        record_and_snapshot,
        set_important,
    )

    db = tmp_path / "net_history.sqlite3"
    one = "AA:BB:CC:00:00:01"
    two = "AA:BB:CC:00:00:02"

    # First populated read seeds the registry — nothing is "new" (no alert spam).
    new, snap = record_and_snapshot([{"mac": one, "ip": "10.0.0.1", "name": "a"}], now=1000, path=db)
    assert new == []
    assert snap[one]["times_seen"] == 1
    assert is_new(snap[one], now=1000) is False  # a seed is never badged new

    # Second read: known device seen again + a brand-new MAC → only the new one alerts.
    new, snap = record_and_snapshot(
        [{"mac": one, "ip": "10.0.0.1", "name": "a"}, {"mac": two, "ip": "10.0.0.2", "name": "b"}],
        now=2000, path=db,
    )
    assert new == [two]
    assert snap[one]["times_seen"] == 2
    assert is_new(snap[two], now=2000) is True       # genuine later arrival → badged
    assert is_new(snap[two], now=2000 + 200_000) is False  # outside the 24 h window

    # Important survives a long absence; a non-important long-absent device prunes.
    set_important(one, True, now=2000, path=db)
    far = 2000 + 200 * 24 * 3600
    _new, snap = record_and_snapshot([], now=far, path=db)
    assert one in snap and snap[one]["important"] is True
    assert two not in snap  # pruned after 180 days unseen
    assert load_network_history(path=db) == snap


def test_network_route_tracks_offline_and_important(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/network`` derives online/offline + the important-offline alert.

    A device present in one read then absent from the next is synthesised as an
    ``online=false`` row; marking it important makes its disappearance alert.
    """
    dev_a = NetDevice(
        mac="5C:CF:7F:AA:BB:CC", ip="192.168.0.5", name=None, conn_type="5GHz",
        signal=60, link_rate=300, ssid="Home", source="ap",
    )
    dev_b = NetDevice(
        mac="B8:27:EB:11:22:33", ip="192.168.0.10", name="nas", conn_type="wired",
        signal=None, link_rate=1000, ssid=None, source="ap",
    )
    base = dict(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, model="R9000", device_count=2),
        router=RouterHealth(reachable=False),
        wifi=WifiDiagnostics(available=False, error="No Wi-Fi adapter"),
        alerts=(),
    )
    holder = {"state": NetworkState(devices=(dev_a, dev_b), **base)}

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return holder["state"]

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )

    # First read seeds the registry: both online, no new-device alert.
    body = client.get("/api/network").json()
    by_mac = {d["mac"]: d for d in body["devices"]}
    assert by_mac["5C:CF:7F:AA:BB:CC"]["online"] is True
    assert by_mac["B8:27:EB:11:22:33"]["online"] is True
    assert not any("New device" in a for a in body["alerts"])

    # Mark the wired host important (lower-case MAC normalises to the store key).
    resp = client.post(
        "/api/network/devices/b8:27:eb:11:22:33/important", json={"important": True}
    )
    assert resp.status_code == 200
    assert resp.json() == {"mac": "B8:27:EB:11:22:33", "important": True}

    # Next read: the important device drops off → offline row + offline alert.
    holder["state"] = NetworkState(devices=(dev_a,), **base)
    body = client.get("/api/network").json()
    by_mac = {d["mac"]: d for d in body["devices"]}
    assert by_mac["5C:CF:7F:AA:BB:CC"]["online"] is True
    offline = by_mac["B8:27:EB:11:22:33"]
    assert offline["online"] is False
    assert offline["important"] is True
    assert offline["source"] == "history"
    assert offline["ip"] == "192.168.0.10"  # last-known IP retained
    assert any("Important device offline" in a for a in body["alerts"])


def test_dhcp_plan_route_classifies_and_assigns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/network/dhcp-plan`` groups devices by category with planned IPs.

    Core fetch + config are faked — no LAN I/O, no on-disk config. The route folds
    the display-name overrides + OUI vendor into the classifier (issue #170).
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="B8:27:EB:11:22:33", ip="192.168.0.50", name="nas",
                conn_type="wired", signal=None, link_rate=1000, ssid=None, source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:CC", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
            NetDevice(
                mac="00:11:22:33:44:55", ip="192.168.0.9", name="mystery",
                conn_type="wired", signal=None, link_rate=100, ssid=None, source="ap",
            ),
        ),
        alerts=(),
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return state

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Infra", 2, 10), CategoryRange("Cameras", 21, 30)),
        rules=(("Infra", ("router", "nas")), ("Cameras", ("camera",))),
        overrides={},
    )

    # The NAS already has a static reservation at its planned IP; the camera does
    # not — so the route should mark them "reserved" vs "create" (issue #176).
    async def fake_fetch_bindings() -> list:
        return [{"name": "nas", "mac": "B8:27:EB:11:22:33", "ip": "192.168.0.2", "inst_id": "X1"}]

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.load_network_display_names", lambda: {}
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.load_dhcp_plan_config", lambda: config
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_dhcp_bindings", fake_fetch_bindings
    )

    resp = client.get("/api/network/dhcp-plan")
    assert resp.status_code == 200
    body = resp.json()

    cats = {c["label"]: c for c in body["categories"]}
    infra = {a["mac"]: a for a in cats["Infra"]["assignments"]}
    cams = {a["mac"]: a for a in cats["Cameras"]["assignments"]}
    # The NAS is reserved at .2 → kept there and flagged already-reserved.
    assert infra["B8:27:EB:11:22:33"]["planned_ip"] == "192.168.0.2"
    assert infra["B8:27:EB:11:22:33"]["status"] == "reserved"
    # The camera has no IP / no binding → lowest free in its range, a new write.
    assert cams["5C:CF:7F:AA:BB:CC"]["planned_ip"] == "192.168.0.21"
    assert cams["5C:CF:7F:AA:BB:CC"]["status"] == "create"
    # Only the camera needs writing; the reserved NAS does not.
    assert body["pending_count"] == 1
    # The unclassified host lands in unassigned, never assigned an IP.
    assert [a["mac"] for a in body["unassigned"]] == ["00:11:22:33:44:55"]
    assert body["unassigned"][0]["planned_ip"] is None
    # The router's live bindings are surfaced for the "on the router now" list
    # (issue #176 step 1): the NAS reservation, flagged online + in the plan.
    assert body["bindings_known"] is True
    existing = {e["mac"]: e for e in body["existing"]}
    assert existing["B8:27:EB:11:22:33"]["online"] is True
    assert existing["B8:27:EB:11:22:33"]["in_plan"] is True
    assert existing["B8:27:EB:11:22:33"]["inst_id"] == "X1"
    # Category labels drive the "assign a group" dropdown on unassigned rows (step 3).
    assert body["category_labels"] == ["Infra", "Cameras"]


def test_dhcp_apply_route_writes_only_pending_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/network/dhcp-plan/apply`` writes create/change rows, not reserved.

    Recomputes the plan server-side and pushes only the rows that need it, one at a
    time, returning a per-row result. Router I/O is monkeypatched — nothing touches
    a real gateway (issue #176).
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="B8:27:EB:11:22:33", ip="192.168.0.2", name="nas",
                conn_type="wired", signal=None, link_rate=1000, ssid=None, source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:CC", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
        ),
        alerts=(),
    )
    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Infra", 2, 10), CategoryRange("Cameras", 21, 30)),
        rules=(("Infra", ("router", "nas")), ("Cameras", ("camera",))),
        overrides={},
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return state

    async def fake_fetch_bindings() -> list:
        # NAS already reserved at its planned .2 → reserved (skip); camera → create.
        return [{"name": "nas", "mac": "B8:27:EB:11:22:33", "ip": "192.168.0.2", "inst_id": "X1"}]

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_network_state", fake_fetch_network_state
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.load_network_display_names", lambda: {}
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.load_dhcp_plan_config", lambda: config
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.fetch_dhcp_bindings", fake_fetch_bindings
    )
    monkeypatch.setattr(
        "app.webapp.routers.network.apply_dhcp_bindings", fake_apply
    )

    resp = client.post("/api/network/dhcp-plan/apply")
    assert resp.status_code == 200
    body = resp.json()

    # Only the camera (create) is written — the reserved NAS is left untouched.
    assert [r["mac"] for r in written] == ["5C:CF:7F:AA:BB:CC"]
    assert written[0]["ip"] == "192.168.0.21"
    assert written[0]["name"]  # a non-empty, router-safe binding name
    assert body["applied"] == 1
    assert body["failed"] == 0
    assert body["results"][0]["ok"] is True


def _two_camera_state() -> "NetworkState":
    """Two unreserved cameras → two ``create`` rows (issue #176 step 2 fixture)."""
    return NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="5C:CF:7F:AA:BB:01", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:02", ip=None, name="back-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
        ),
        alerts=(),
    )


def test_dhcp_apply_route_selective_writes_only_listed_macs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../apply`` with a ``{"macs": [...]}`` body writes only those rows.

    Selective apply (issue #176 step 2): the user ticks which create/change rows to
    push when the 10-slot table can't hold them all. The plan is recomputed
    server-side; only the listed MAC is written. Router I/O is monkeypatched.
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(("Cameras", ("camera",)),),
        overrides={},
    )

    async def fake_state(include_speedtest: bool = False) -> NetworkState:
        return _two_camera_state()

    async def fake_bindings() -> list:
        return []

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", fake_state)
    monkeypatch.setattr("app.webapp.routers.network.load_network_display_names", lambda: {})
    monkeypatch.setattr("app.webapp.routers.network.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.network.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.network.apply_dhcp_bindings", fake_apply)

    # Lower-case input proves the MAC allow-list is normalised before matching.
    resp = client.post(
        "/api/network/dhcp-plan/apply", json={"macs": ["5c:cf:7f:aa:bb:01"]}
    )
    assert resp.status_code == 200
    assert [r["mac"] for r in written] == ["5C:CF:7F:AA:BB:01"]
    assert resp.json()["applied"] == 1


def test_dhcp_delete_route_validates_and_calls_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-bindings/delete`` frees a slot by ``inst_id`` (issue #176 step 1)."""
    captured: dict = {}

    async def fake_delete(inst_id: str) -> bool:
        captured["inst_id"] = inst_id
        return True

    monkeypatch.setattr("app.webapp.routers.network.delete_dhcp_binding", fake_delete)

    resp = client.post(
        "/api/network/dhcp-bindings/delete", json={"inst_id": "DEV.V4DP.Sr.Pl1.Bd3"}
    )
    assert resp.status_code == 200
    assert captured["inst_id"] == "DEV.V4DP.Sr.Pl1.Bd3"
    # A malformed id is rejected (400) before any router call.
    resp_bad = client.post(
        "/api/network/dhcp-bindings/delete", json={"inst_id": "bad id; rm -rf"}
    )
    assert resp_bad.status_code == 400


def test_dhcp_override_route_persists_and_validates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PUT .../dhcp-overrides/{mac}`` saves a per-MAC group choice (issue #176 step 3)."""
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(),
        overrides={},
    )
    saved: dict = {}

    def fake_set(mac: str, category: str) -> None:
        saved[mac] = category

    monkeypatch.setattr("app.webapp.routers.network.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.network.set_dhcp_override", fake_set)

    resp = client.put(
        "/api/network/dhcp-overrides/5C:CF:7F:AA:BB:01", json={"category": "Cameras"}
    )
    assert resp.status_code == 200
    assert saved == {"5C:CF:7F:AA:BB:01": "Cameras"}
    # An unknown category is rejected (400) — must be one of the configured ranges.
    resp_bad = client.put(
        "/api/network/dhcp-overrides/5C:CF:7F:AA:BB:01", json={"category": "Nope"}
    )
    assert resp_bad.status_code == 400


def test_dhcp_manual_add_validates_and_writes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-bindings`` adds one validated reservation (issue #176 step 3)."""
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(),
        overrides={},
    )

    async def fake_bindings() -> list:
        return [{"name": "x", "mac": "00:00:00:00:00:09", "ip": "192.168.0.9", "inst_id": "B1"}]

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr("app.webapp.routers.network.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.network.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.network.apply_dhcp_bindings", fake_apply)

    # A wrong-subnet IP is rejected (400) before any router call.
    bad = client.post(
        "/api/network/dhcp-bindings", json={"mac": "AA:BB:CC:DD:EE:01", "ip": "10.0.0.5"}
    )
    assert bad.status_code == 400
    # An IP already held by a *different* MAC is a conflict (409).
    dup = client.post(
        "/api/network/dhcp-bindings", json={"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.9"}
    )
    assert dup.status_code == 409
    # A valid add writes exactly one binding row.
    good = client.post(
        "/api/network/dhcp-bindings",
        json={"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.25", "name": "Cam"},
    )
    assert good.status_code == 200
    assert [r["mac"] for r in written] == ["AA:BB:CC:DD:EE:01"]
    assert written[0]["ip"] == "192.168.0.25"


def test_dhcp_reservations_apply_removes_then_adds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-reservations/apply`` applies a staged batch (issue #176 redesign).

    The user marks router rows to remove, plan rows + manual rows to add; the route
    recomputes the plan MACs' IPs server-side and hands one combined batch to
    ``apply_dhcp_changes`` (deletes first, then adds). Router I/O is monkeypatched.
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(("Cameras", ("camera",)),),
        overrides={},
    )

    async def fake_state(include_speedtest: bool = False) -> NetworkState:
        return _two_camera_state()

    async def fake_bindings() -> list:
        return [{"name": "old", "mac": "00:00:00:00:00:09", "ip": "192.168.0.9", "inst_id": "B9"}]

    captured: dict = {}

    async def fake_changes(removes: list, adds: list) -> list:
        captured["removes"] = list(removes)
        captured["adds"] = list(adds)
        out = [{"op": "remove", "inst_id": r, "ok": True, "error": None} for r in removes]
        out += [{"op": "add", "mac": a["mac"], "ip": a["ip"], "ok": True, "error": None} for a in adds]
        return out

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", fake_state)
    monkeypatch.setattr("app.webapp.routers.network.load_network_display_names", lambda: {})
    monkeypatch.setattr("app.webapp.routers.network.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.network.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.network.apply_dhcp_changes", fake_changes)

    resp = client.post(
        "/api/network/dhcp-reservations/apply",
        json={
            "remove": ["B9"],
            "add_macs": ["5c:cf:7f:aa:bb:01"],
            "add_manual": [{"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.40", "name": "Manual"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # The remove is passed through; both a plan MAC and a manual row are added.
    assert captured["removes"] == ["B9"]
    add_macs = [a["mac"] for a in captured["adds"]]
    assert "5C:CF:7F:AA:BB:01" in add_macs and "AA:BB:CC:DD:EE:01" in add_macs
    # The plan row's IP is recomputed server-side (lowest free in the camera range).
    plan_row = next(a for a in captured["adds"] if a["mac"] == "5C:CF:7F:AA:BB:01")
    assert plan_row["ip"] == "192.168.0.21"
    assert body["removed"] == 1 and body["added"] == 2 and body["failed"] == 0
    # A bad manual IP is rejected (400) before any router call.
    bad = client.post(
        "/api/network/dhcp-reservations/apply",
        json={"add_manual": [{"mac": "AA:BB:CC:DD:EE:02", "ip": "10.0.0.1"}]},
    )
    assert bad.status_code == 400


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


def test_security_zone_trouble_ignore(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Zone payload exposes trouble_ignored; PUT persists it via the store (#225)."""
    import src.security_trouble_ignore as ti
    from src.risco_client import SecurityState, SecurityZone

    monkeypatch.setattr(ti, "DEFAULT_PATH", tmp_path / "security_trouble_ignore.json")

    async def fake_state() -> SecurityState:
        return SecurityState(
            reachable=True, label="Disarmed", mode="disarmed",
            zones=[SecurityZone(id=3, name="3", trouble=True)],
        )

    monkeypatch.setattr("app.webapp.routers.security.fetch_security_state", fake_state)

    # Default: troubled but not ignored.
    body = client.get("/api/security").json()
    zone = body["zones"][0]
    assert zone["trouble"] is True and zone["trouble_ignored"] is False

    # Ignore it → persisted.
    put = client.put("/api/security/zones/3/trouble_ignored", json={"ignored": True})
    assert put.status_code == 200
    assert put.json() == {"zone_id": 3, "trouble_ignored": True}
    assert ti.load_ignored_trouble_zone_ids(tmp_path / "security_trouble_ignore.json") == {"3"}

    # Subsequent reads reflect it.
    body = client.get("/api/security").json()
    assert body["zones"][0]["trouble_ignored"] is True


# ── Issue #247: control-unit input validation + temperature clamping ───────────


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
