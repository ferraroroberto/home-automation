"""Network client host-side probes."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from src import network_client


def test_ping_hides_windows_console(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "Packets: Sent = 2, Received = 2, Lost = 0 (0% loss),\n"
                "Minimum = 1ms, Maximum = 2ms, Average = 2ms\n"
            ),
        )

    monkeypatch.setattr(network_client.sys, "platform", "win32")
    monkeypatch.setattr(network_client.subprocess, "run", fake_run)

    avg_ms, loss_pct = network_client._ping("192.0.2.1", count=2, timeout_s=1)

    assert avg_ms == 2.0
    assert loss_pct == 0.0
    assert calls[0]["cmd"] == ["ping", "-n", "2", "-w", "1000", "192.0.2.1"]
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_parse_wifi_netsh_outputs() -> None:
    interfaces = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi
    State                  : connected
    SSID                   : HomeNet
    BSSID                  : aa:bb:cc:dd:ee:01
    Radio type             : 802.11ac
    Channel                : 44
    Signal                 : 86%
"""
    networks = """
Interface name : Wi-Fi

SSID 1 : HomeNet
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : aa:bb:cc:dd:ee:01
         Signal             : 86%
         Radio type         : 802.11ac
         Band               : 5 GHz
         Channel            : 44
    BSSID 2                 : aa:bb:cc:dd:ee:02
         Signal             : 55%
         Radio type         : 802.11n
         Band               : 2.4 GHz
         Channel            : 6
"""

    current = network_client._parse_wifi_interfaces(interfaces)
    bssids = network_client._parse_wifi_networks(networks, current)

    assert current["name"] == "Wi-Fi"
    assert current["bssid"] == "AA:BB:CC:DD:EE:01"
    assert current["signal"] == "86%"
    assert bssids[0].ssid == "HomeNet"
    assert bssids[0].bssid == "AA:BB:CC:DD:EE:01"
    assert bssids[0].connected is True
    assert bssids[0].signal == 86
    assert bssids[0].rssi_dbm == -57
    assert bssids[0].band == "5GHz"
    assert bssids[1].band == "2.4GHz"


def test_fetch_network_state_returns_partial_data_when_source_times_out(monkeypatch) -> None:
    async def fake_internet_health(include_speedtest: bool = False):
        return network_client.InternetHealth(online=True, external_ms=12)

    async def slow_access_point():
        await asyncio.sleep(1)
        return network_client.AccessPointHealth(reachable=True), [
            network_client.NetDevice(
                mac="AA:BB:CC:DD:EE:FF",
                ip="192.0.2.20",
                name="Late device",
                conn_type="5GHz",
                signal=80,
                link_rate=866,
                ssid="LateNet",
                source="ap",
            )
        ]

    async def fake_router():
        return network_client.RouterHealth(reachable=True, authenticated=True)

    async def fake_wifi():
        return network_client.WifiDiagnostics(
            available=True,
            interface_name="Wi-Fi",
            bssids=(
                network_client.WifiBssid(
                    ssid="HomeNet",
                    bssid="AA:BB:CC:DD:EE:01",
                    signal=86,
                    rssi_dbm=-57,
                    channel=44,
                    band="5GHz",
                    radio_type="802.11ac",
                    authentication="WPA2-Personal",
                    encryption="CCMP",
                ),
            ),
        )

    monkeypatch.setattr(network_client, "_ACCESS_POINT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(network_client, "fetch_internet_health", fake_internet_health)
    monkeypatch.setattr(network_client, "fetch_access_point", slow_access_point)
    monkeypatch.setattr(network_client, "fetch_router", fake_router)
    monkeypatch.setattr(network_client, "fetch_wifi_diagnostics", fake_wifi)

    state = asyncio.run(network_client.fetch_network_state())

    assert state.internet.online is True
    assert state.access_point.reachable is False
    assert state.access_point.error == "read timed out"
    assert state.router.authenticated is True
    assert state.wifi.available is True
    assert state.wifi.bssids[0].ssid == "HomeNet"
    assert state.devices == ()
