"""Device-group API tests (issue #513).

Drives the real ``/api/network`` routes with the cloud fetchers monkeypatched:
assignment persists and merges into the snapshot, offline rows keep their group
plus their last-known band/SSID, and rename/delete move every member without
losing a device.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.network_client import (
    AccessPointHealth,
    InternetHealth,
    NetDevice,
    NetworkState,
    RouterHealth,
    WifiDiagnostics,
)


def _state(*devices: NetDevice) -> NetworkState:
    return NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=len(devices)),
        router=RouterHealth(reachable=True, authenticated=True),
        devices=devices,
        wifi=WifiDiagnostics(available=False),
        alerts=(),
    )


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Isolate the group store and serve a two-device fixture LAN."""
    import src.network_groups as ng

    monkeypatch.setattr(ng, "DEFAULT_PATH", tmp_path / "network_groups.json")

    devices = (
        NetDevice(
            mac="A4:CF:12:00:00:01",
            ip="192.0.2.11",
            name="Light one",
            conn_type="2.4GHz",
            signal=70,
            link_rate=72,
            ssid="Home-IoT",
            source="ap",
        ),
        NetDevice(
            mac="A4:CF:12:00:00:02",
            ip="192.0.2.12",
            name="Light two",
            conn_type="2.4GHz",
            signal=60,
            link_rate=72,
            ssid="Home-IoT",
            source="ap",
        ),
    )

    async def fake_fetch(include_speedtest: bool = False) -> NetworkState:
        return _state(*devices)

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", fake_fetch)
    return devices


def _devices_by_mac(client: TestClient):
    body = client.get("/api/network").json()
    return {d["mac"]: d for d in body["devices"]}


def test_group_assignment_merges_into_the_snapshot(client: TestClient, net) -> None:
    resp = client.put(
        "/api/network/devices/a4:cf:12:00:00:01/group",
        json={"group": "  Elgato lights  "},
    )
    assert resp.status_code == 200
    assert resp.json() == {"mac": "A4:CF:12:00:00:01", "group": "Elgato lights"}

    devices = _devices_by_mac(client)
    assert devices["A4:CF:12:00:00:01"]["group"] == "Elgato lights"
    # No assignment → null, which is what puts a device under Unclassified.
    assert devices["A4:CF:12:00:00:02"]["group"] is None

    # Clearing sends it back to Unclassified.
    resp = client.put("/api/network/devices/A4:CF:12:00:00:01/group", json={"group": ""})
    assert resp.json() == {"mac": "A4:CF:12:00:00:01", "group": None}
    assert _devices_by_mac(client)["A4:CF:12:00:00:01"]["group"] is None


def test_rename_and_delete_move_members_without_losing_devices(
    client: TestClient, net
) -> None:
    for mac in ("A4:CF:12:00:00:01", "A4:CF:12:00:00:02"):
        client.put(f"/api/network/devices/{mac}/group", json={"group": "Lights"})

    resp = client.put(
        "/api/network/groups/rename", json={"name": "Lights", "new_name": "Elgato lights"}
    )
    assert resp.status_code == 200
    assert resp.json()["moved"] == 2
    assert {d["group"] for d in _devices_by_mac(client).values()} == {"Elgato lights"}

    resp = client.post("/api/network/groups/delete", json={"name": "Elgato lights"})
    assert resp.status_code == 200
    assert resp.json() == {"name": "Elgato lights", "moved": 2}

    devices = _devices_by_mac(client)
    # Both devices are still present — only the assignment went away.
    assert len(devices) == 2
    assert all(d["group"] is None for d in devices.values())


def test_group_endpoints_reject_blank_names(client: TestClient, net) -> None:
    assert client.put(
        "/api/network/groups/rename", json={"name": "Lights", "new_name": "  "}
    ).status_code == 400
    assert client.post(
        "/api/network/groups/delete", json={"name": "  "}
    ).status_code == 400


def test_offline_row_keeps_its_group_and_last_known_band(
    client: TestClient, net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device absent from the read still renders with band/SSID + its group."""
    client.put(
        "/api/network/devices/A4:CF:12:00:00:01/group", json={"group": "Elgato lights"}
    )
    # First read records both devices (band + SSID included).
    assert len(_devices_by_mac(client)) == 2

    async def only_second(include_speedtest: bool = False) -> NetworkState:
        return _state(net[1])

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", only_second)

    devices = _devices_by_mac(client)
    gone = devices["A4:CF:12:00:00:01"]
    assert gone["online"] is False
    assert gone["group"] == "Elgato lights"
    # Live fields are null (we no longer observe it) but the last-known band and
    # SSID ride along so the grouped row stays readable.
    assert gone["conn_type"] is None
    assert gone["last_conn_type"] == "2.4GHz"
    assert gone["last_ssid"] == "Home-IoT"
