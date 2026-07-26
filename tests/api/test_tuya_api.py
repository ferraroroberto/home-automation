"""API smoke for the Tuya backoff surfacing (issue #537).

``GET /api/tuya`` must report a device skipped for backoff distinctly from a
device that was actually dialed and refused — no real LAN I/O in either case.
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
