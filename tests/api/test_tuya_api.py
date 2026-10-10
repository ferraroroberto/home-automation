"""API smoke for the Tuya endpoints.

``GET /api/tuya`` must report a device skipped for backoff distinctly from a
device that was actually dialed and refused (issue #537), and
``POST /api/tuya/pair`` must capture newly-paired devices from the Tuya cloud
and fail with actionable text when it can't (issue #612). No real LAN or
cloud I/O in any of it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _write_devices(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_backed_off_device_reports_distinct_error(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.tuya_client as tuya_client

    path = tmp_path / "devices.json"
    _write_devices(
        path,
        [
            {
                "id": "dev-1",
                "name": "Test plug",
                "ip": "192.168.0.50",
                "key": "secret",
                "version": "3.3",
                "mapping": {"1": {"code": "switch_1"}},
            }
        ],
    )
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", path)
    tuya_client._backoff_state.clear()
    tuya_client._record_backoff_failure("dev-1")

    def _status_should_not_be_called(*_a, **_kw):
        raise AssertionError("_status must not be called while backed off")

    monkeypatch.setattr(tuya_client, "_status", _status_should_not_be_called)

    body = client.get("/api/tuya").json()

    assert len(body["devices"]) == 1
    device = body["devices"][0]
    assert device["reachable"] is False
    assert "backing off" in device["error"]

    tuya_client._backoff_state.clear()


def _stub_devices_file(monkeypatch, tmp_path, rows) -> "object":
    """Point the Tuya client at a throwaway devices.json holding ``rows``."""
    import src.tuya_client as tuya_client

    path = tmp_path / "devices.json"
    _write_devices(path, rows)
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", path)
    tuya_client._backoff_state.clear()
    return path


def test_pair_captures_a_new_device_and_rescans_the_lan(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """The happy path: a plug paired in Smart Life lands on the Plugs card."""
    import src.tuya_client as tuya_client
    import src.tuya_cloud as tuya_cloud
    from app.webapp.routers import tuya as tuya_router

    path = _stub_devices_file(
        monkeypatch, tmp_path, [{"id": "dev-1", "name": "Known", "key": "k1"}]
    )

    monkeypatch.setattr(
        tuya_cloud,
        "_fetch_cloud_rows",
        lambda entries: [
            {"id": "dev-1", "name": "Known", "key": "k1"},
            {"id": "dev-2", "name": "New plug", "key": "k2", "mapping": {"1": {"code": "switch_1"}}},
        ],
    )
    # A new device has no address yet, so the endpoint must follow the sync
    # with the existing LAN scan rather than leaving it unreachable.
    scanned: list[bool] = []
    monkeypatch.setattr(
        tuya_router,
        "rescan_addresses",
        lambda *_a, **_kw: scanned.append(True) or {"found": 2, "updated": [], "addresses": {}},
    )
    monkeypatch.setattr(
        tuya_client, "_status", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no LAN I/O"))
    )

    body = client.post("/api/tuya/pair").json()

    assert scanned == [True]
    assert body["pair"]["added"] == ["dev-2"]
    assert body["pair"]["found"] == 2
    assert "Added 1 new device" in body["pair"]["detail"]
    assert sorted(d["device_id"] for d in body["devices"]) == ["dev-1", "dev-2"]
    # The merge is persisted, not just reported.
    assert [row["id"] for row in json.loads(path.read_text(encoding="utf-8"))] == [
        "dev-1",
        "dev-2",
    ]


def test_pair_still_rescans_when_the_cloud_has_nothing_new(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """Add is the only LAN-rediscovery path left, so the scan is unconditional.

    A plug that merely took a new DHCP lease must be recoverable here even
    though the cloud reports no new devices (the Refresh button is gone).
    """
    import src.tuya_cloud as tuya_cloud
    from app.webapp.routers import tuya as tuya_router

    _stub_devices_file(monkeypatch, tmp_path, [{"id": "dev-1", "name": "Known", "key": "k1"}])
    monkeypatch.setattr(
        tuya_cloud, "_fetch_cloud_rows", lambda entries: [{"id": "dev-1", "name": "Known", "key": "k1"}]
    )
    monkeypatch.setattr(
        tuya_router,
        "rescan_addresses",
        lambda *_a, **_kw: {"found": 3, "updated": ["dev-1"], "addresses": {}},
    )

    body = client.post("/api/tuya/pair").json()

    assert body["pair"]["added"] == []
    assert body["pair"]["recovered"] == ["dev-1"]
    assert "No new devices" in body["pair"]["detail"]
    assert "recovered 1 stale" in body["pair"]["detail"]
    assert "Smart Life" in body["pair"]["detail"]


def test_pair_survives_a_failed_lan_scan(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """A scan failure must not sink a cloud sync that already landed."""
    import src.tuya_cloud as tuya_cloud
    from app.webapp.routers import tuya as tuya_router

    _stub_devices_file(monkeypatch, tmp_path, [{"id": "dev-1", "name": "Known", "key": "k1"}])
    monkeypatch.setattr(tuya_cloud, "_fetch_cloud_rows", lambda entries: [])

    def _boom(*_a, **_kw):
        raise OSError("no route to broadcast address")

    monkeypatch.setattr(tuya_router, "rescan_addresses", _boom)

    response = client.post("/api/tuya/pair")

    assert response.status_code == 200
    assert "LAN scan failed" in response.json()["pair"]["detail"]


def test_pair_surfaces_a_cloud_failure_as_actionable_503(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.tuya_cloud as tuya_cloud

    _stub_devices_file(monkeypatch, tmp_path, [{"id": "dev-1", "name": "Known", "key": "k1"}])

    def _expired(*_a, **_kw):
        raise tuya_cloud.TuyaCloudError("IoT Core subscription expired — renew it at https://iot.tuya.com.")

    monkeypatch.setattr(tuya_cloud, "_fetch_cloud_rows", _expired)

    response = client.post("/api/tuya/pair")

    assert response.status_code == 503
    assert "iot.tuya.com" in response.json()["detail"]


def test_pair_surfaces_an_unexpected_failure_as_502(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    import src.tuya_cloud as tuya_cloud

    _stub_devices_file(monkeypatch, tmp_path, [{"id": "dev-1", "name": "Known", "key": "k1"}])

    def _boom(*_a, **_kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(tuya_cloud, "_fetch_cloud_rows", _boom)

    response = client.post("/api/tuya/pair")

    assert response.status_code == 502
    assert "connection reset" in response.json()["detail"]


def test_tuya_route_surfaces_no_ip_identity_and_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No-IP Tuya rows stay visible with stable non-secret identity metadata."""
    import json

    import src.tuya_client as tuya_client
    from src.tuya_client import TuyaDeviceInfo

    # ``POST /api/tuya/pair`` reads ``devices.json`` off disk (issue #621) —
    # point it at a throwaway file so the test never depends on whether the
    # real, gitignored one happens to exist in the checkout running it.
    devices_file = tmp_path / "devices.json"
    devices_file.write_text(
        json.dumps([{"id": "plug-noip", "name": "Fixture Plug", "key": "k"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(tuya_client, "_DEVICE_FILE", devices_file)

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

    # Add (the only remaining rediscovery path, #612) runs a LAN rescan
    # server-side; fake both it and the cloud so the test never leaves the box.
    monkeypatch.setattr(
        "app.webapp.routers.tuya.rescan_addresses",
        lambda: {"found": 2, "updated": ["plug-noip"], "addresses": {"plug-noip": "192.0.2.7"}},
    )
    monkeypatch.setattr("src.tuya_cloud._fetch_cloud_rows", lambda entries: [])
    paired = client.post("/api/tuya/pair")
    assert paired.status_code == 200
    pair = paired.json()["pair"]
    assert pair["found"] == 2
    assert pair["recovered"] == ["plug-noip"]
    assert "recovered 1 stale" in pair["detail"]


def _stub_blinds(monkeypatch: pytest.MonkeyPatch, fail: tuple[str, ...] = ()) -> list:
    """Two fake blinds and a plug; ``set_cover`` records instead of dialing."""
    import src.blind_automation as blind_automation
    from src.tuya_client import TuyaCommandError, TuyaDeviceInfo

    infos = [
        TuyaDeviceInfo(device_id="blind-1", name="Blind 1", cover_control_dps="1"),
        TuyaDeviceInfo(device_id="blind-2", name="Blind 2", cover_control_dps="1"),
        TuyaDeviceInfo(device_id="plug-1", name="Plug 1", switch_dps="1"),
    ]
    monkeypatch.setattr(blind_automation, "list_devices", lambda: infos)
    sent: list = []

    def _set_cover(device_id: str, action: str) -> dict:
        if device_id in fail:
            raise TuyaCommandError(f"{device_id} did not answer")
        sent.append((device_id, action))
        return {}

    monkeypatch.setattr(blind_automation, "set_cover", _set_cover)
    return sent


def test_cover_group_moves_every_blind_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _stub_blinds(monkeypatch)
    response = client.post("/api/tuya/covers", json={"action": "open"})
    assert response.status_code == 200
    body = response.json()
    assert body["failed"] == 0
    assert sorted(sent) == [("blind-1", "open"), ("blind-2", "open")]
    assert {r["device_id"] for r in body["results"]} == {"blind-1", "blind-2"}


def test_cover_group_reports_a_partial_failure_per_blind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _stub_blinds(monkeypatch, fail=("blind-2",))
    _spy_settle(monkeypatch)
    response = client.post(
        "/api/tuya/covers", json={"action": "close", "device_ids": ["blind-1", "blind-2"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["failed"] == 1
    failed = [r for r in body["results"] if not r["ok"]]
    assert failed[0]["device_id"] == "blind-2"
    assert "did not answer" in failed[0]["error"]
    assert sent == [("blind-1", "close")]


def _spy_settle(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record what each manual tap hands to the background retry (#899)."""
    settled: list = []

    async def _settle(action, outcomes):
        settled.append((action, {o.device_id: o.ok for o in outcomes}))

    monkeypatch.setattr("app.webapp.routers.tuya.settle_manual_move", _settle)
    return settled


def test_manual_cover_group_tap_hands_its_failures_to_the_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual group tap answers with its first attempt, then retries (#899)."""
    _stub_blinds(monkeypatch, fail=("blind-2",))
    settled = _spy_settle(monkeypatch)
    response = client.post("/api/tuya/covers", json={"action": "open"})
    assert response.status_code == 200 and response.json()["failed"] == 1
    assert settled == [("open", {"blind-1": True, "blind-2": False})]


def test_manual_single_cover_tap_retries_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_blinds(monkeypatch, fail=("blind-2",))
    settled = _spy_settle(monkeypatch)
    response = client.post("/api/tuya/blind-2/cover", json={"action": "close"})
    assert response.status_code == 502
    assert "did not answer; retrying" in response.json()["detail"]
    assert settled == [("close", {"blind-2": False})]


def test_manual_single_cover_tap_rejects_a_device_that_is_not_a_blind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _stub_blinds(monkeypatch)
    response = client.post("/api/tuya/plug-1/cover", json={"action": "open"})
    assert response.status_code == 404 and sent == []


def test_manual_single_cover_tap_supersedes_a_pending_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import src.blind_automation as blind_automation

    _stub_blinds(monkeypatch, fail=("blind-2",))
    outcomes = asyncio.run(blind_automation.move_blinds("open"))
    pending = next(o for o in outcomes if not o.ok)
    assert blind_automation.is_newest_command("blind-2", pending.command)
    _stub_blinds(monkeypatch)  # the blind answers again
    _spy_settle(monkeypatch)
    response = client.post("/api/tuya/blind-2/cover", json={"action": "stop"})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert not blind_automation.is_newest_command("blind-2", pending.command)


def test_cover_group_is_502_only_when_every_blind_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_blinds(monkeypatch, fail=("blind-1", "blind-2"))
    settled = _spy_settle(monkeypatch)
    response = client.post("/api/tuya/covers", json={"action": "stop"})
    assert response.status_code == 502
    assert "no blind accepted stop, retrying" in response.json()["detail"]
    assert settled == [("stop", {"blind-1": False, "blind-2": False})]


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"action": "raise"}, "action must be"),
        ({"action": "open", "device_ids": ["plug-1"]}, "not a blind: plug-1"),
    ],
)
def test_cover_group_rejects_caller_bugs_without_moving_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict, detail: str
) -> None:
    sent = _stub_blinds(monkeypatch)
    response = client.post("/api/tuya/covers", json=payload)
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert sent == []


def test_tuya_card_carries_the_light_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_devices_file(
        monkeypatch,
        tmp_path,
        [
            {"id": "light-1", "name": "Fixture light", "category": "dj", "key": "k",
             "mapping": {"1": {"code": "switch_led"}}},
            {"id": "plug-1", "name": "Fixture plug", "category": "cz", "key": "k",
             "mapping": {"1": {"code": "switch_1"}}},
        ],
    )
    cards = {c["device_id"]: c for c in client.get("/api/tuya").json()["devices"]}
    assert cards["light-1"]["is_light"] is True
    assert cards["plug-1"]["is_light"] is False


_DIMMER_ROW = {
    "id": "dimmer-1", "name": "Fixture dimmer", "category": "dj", "key": "k",
    "mapping": {
        "1": {"code": "switch_led", "type": "Boolean", "values": {}},
        "2": {"code": "bright_value", "type": "Integer", "values": {"min": 25, "max": 255}},
    },
}


def test_brightness_endpoint_writes_and_reads_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import app.webapp.routers.tuya as tuya_router

    _stub_devices_file(monkeypatch, tmp_path, [_DIMMER_ROW])
    sent: list = []
    monkeypatch.setattr(tuya_router, "set_brightness", lambda d, pct: sent.append((d, pct)))
    response = client.post("/api/tuya/dimmer-1/brightness", json={"brightness": 40})
    assert response.status_code == 200
    assert sent == [("dimmer-1", 40)]
    card = response.json()
    assert card["device_id"] == "dimmer-1"
    assert card["has_brightness"] is True


def test_brightness_endpoint_maps_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src.tuya_client import TuyaCommandError

    _stub_devices_file(
        monkeypatch, tmp_path,
        [_DIMMER_ROW, {"id": "plug-1", "key": "k", "mapping": {"1": {"code": "switch_1"}}}],
    )
    assert client.post("/api/tuya/dimmer-1/brightness", json={"brightness": 0}).status_code == 400
    no_dps = client.post("/api/tuya/plug-1/brightness", json={"brightness": 50})
    assert no_dps.status_code == 400
    assert "no brightness" in no_dps.json()["detail"]
    assert client.post("/api/tuya/ghost/brightness", json={"brightness": 50}).status_code == 404

    import app.webapp.routers.tuya as tuya_router

    def _offline(device_id, pct):
        raise TuyaCommandError("no response on the LAN")

    monkeypatch.setattr(tuya_router, "set_brightness", _offline)
    offline = client.post("/api/tuya/dimmer-1/brightness", json={"brightness": 50})
    assert offline.status_code == 502


def test_tuya_card_reports_brightness_capability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_devices_file(
        monkeypatch, tmp_path,
        [_DIMMER_ROW, {"id": "plug-1", "key": "k", "mapping": {"1": {"code": "switch_1"}}}],
    )
    cards = {c["device_id"]: c for c in client.get("/api/tuya").json()["devices"]}
    assert cards["dimmer-1"]["has_brightness"] is True
    assert cards["plug-1"]["has_brightness"] is False
