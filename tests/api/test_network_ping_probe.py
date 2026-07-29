"""Network device ping-probe API surface (issue #552).

Drives the real ``GET /api/network`` route with ``fetch_network_state``
monkeypatched to a fixture LAN, asserting the flattened ``ping_reachable``
field rides through for both live and synthesised-offline device rows.
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


def _devices_by_mac(client: TestClient):
    body = client.get("/api/network").json()
    return {d["mac"]: d for d in body["devices"]}


def test_ping_reachable_field_rides_through_the_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    devices = (
        NetDevice(
            mac="A4:CF:12:00:01:01",
            ip="192.0.2.21",
            name="Confirmed by probe",
            conn_type=None,
            signal=None,
            link_rate=None,
            ssid=None,
            source="router",
            ping_reachable=True,
        ),
        NetDevice(
            mac="A4:CF:12:00:01:02",
            ip="192.0.2.22",
            name="Probed, unreachable",
            conn_type=None,
            signal=None,
            link_rate=None,
            ssid=None,
            source="router",
            ping_reachable=False,
        ),
        NetDevice(
            mac="A4:CF:12:00:01:03",
            ip="192.0.2.23",
            name="Ordinary live client",
            conn_type="5GHz",
            signal=70,
            link_rate=866,
            ssid="HomeNet",
            source="ap",
        ),
    )

    async def fake_fetch(include_speedtest: bool = False) -> NetworkState:
        return _state(*devices)

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", fake_fetch)

    by_mac = _devices_by_mac(client)
    assert by_mac["A4:CF:12:00:01:01"]["ping_reachable"] is True
    assert by_mac["A4:CF:12:00:01:02"]["ping_reachable"] is False
    # A device with its own AP/router evidence is never probed — null, not False.
    assert by_mac["A4:CF:12:00:01:03"]["ping_reachable"] is None
