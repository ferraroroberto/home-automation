"""API smoke for the Tuya endpoints.

``GET /api/tuya`` must report a device skipped for backoff distinctly from a
device that was actually dialed and refused (issue #537), and
``POST /api/tuya/pair`` must capture newly-paired devices from the Tuya cloud
and fail with actionable text when it can't (issue #612). No real LAN or
cloud I/O in any of it.
"""

from __future__ import annotations

import json

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
