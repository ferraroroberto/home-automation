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


def test_wifi_channel_insights_rank_candidates_and_coordinate_pairs() -> None:
    bssids = [
        network_client.WifiBssid(
            ssid="REDWIFI",
            bssid="AA:BB:CC:DD:EE:08",
            signal=80,
            rssi_dbm=-60,
            channel=8,
            band="2.4GHz",
            radio_type="802.11ax",
            authentication="WPA3-Personal",
            encryption="CCMP",
        ),
        network_client.WifiBssid(
            ssid="MOVISTAR",
            bssid="AA:BB:CC:DD:EE:13",
            signal=57,
            rssi_dbm=-71,
            channel=13,
            band="2.4GHz",
            radio_type="802.11ac",
            authentication="WPA2-Personal",
            encryption="CCMP",
        ),
        network_client.WifiBssid(
            ssid="Printer",
            bssid="AA:BB:CC:DD:EE:14",
            signal=85,
            rssi_dbm=-57,
            channel=13,
            band="2.4GHz",
            radio_type="802.11n",
            authentication="WPA2-Personal",
            encryption="CCMP",
        ),
        network_client.WifiBssid(
            ssid="Neighbour",
            bssid="AA:BB:CC:DD:EE:03",
            signal=66,
            rssi_dbm=-67,
            channel=3,
            band="2.4GHz",
            radio_type="802.11n",
            authentication="WPA2-Personal",
            encryption="CCMP",
        ),
        network_client.WifiBssid(
            ssid="Neighbour-5",
            bssid="AA:BB:CC:DD:EE:48",
            signal=45,
            rssi_dbm=-77,
            channel=48,
            band="5GHz",
            radio_type="802.11ac",
            authentication="WPA2-Personal",
            encryption="CCMP",
        ),
    ]

    insights = network_client._wifi_channel_insights(bssids)

    insight_24 = next(i for i in insights if i.band == "2.4GHz")
    assert insight_24.recommended_width_mhz == 20
    assert insight_24.recommended_channel == 1
    assert insight_24.coordinated_channels == (1, 6)
    assert insight_24.candidate_scores[0].channel == 1
    assert insight_24.apply_supported is False

    tips = network_client._wifi_recommendations(None, None, bssids, insights)
    channel_tips = [tip for tip in tips if tip.startswith("Least-crowded channels:")]
    assert len(channel_tips) == 1
    assert "2.4GHz ch 1 at 20 MHz" in channel_tips[0]
    assert "5GHz ch" in channel_tips[0]


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
        return network_client.RouterHealth(reachable=True, authenticated=True), []

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


def _ap_device(mac: str, name, **kw) -> "network_client.NetDevice":
    base = dict(
        ip="192.168.0.10", name=name, conn_type="5GHz", signal=70,
        link_rate=866, ssid="HomeNet", source="ap",
    )
    base.update(kw)
    return network_client.NetDevice(mac=mac, **base)


def test_merge_router_leases_fills_names_and_adds_router_only() -> None:
    devices = [
        _ap_device("AA:BB:CC:00:00:01", None),                 # no name → fill it
        _ap_device("AA:BB:CC:00:00:02", "Laptop"),             # has name → keep it
    ]
    leases = [
        {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.0.58", "hostname": "SMA1930031140"},
        {"mac": "aa:bb:cc:00:00:02", "ip": "192.168.0.59", "hostname": "router-name"},
        {"mac": "34:5a:60:d3:59:53", "ip": "192.168.0.66", "hostname": "tower"},  # router-only
        {"mac": "02:0f:b5:eb:fb:fd", "ip": "192.168.0.99", "hostname": None},     # router-only, no host
    ]

    merged = network_client._merge_router_leases(devices, leases)
    by_mac = {network_client._normalise_mac(d.mac): d for d in merged}

    # Lowercase router MAC dedups against the uppercase AP MAC (no duplicate row).
    assert len(merged) == 4
    # Missing AP name filled from the router hostname; corroborated → "both".
    filled = by_mac["AA:BB:CC:00:00:01"]
    assert filled.name == "SMA1930031140"
    assert filled.source == "both"
    # An AP-reported name is never overwritten, but the device is still "both".
    kept = by_mac["AA:BB:CC:00:00:02"]
    assert kept.name == "Laptop"
    assert kept.source == "both"
    # Router-only device added with source="router" and unknown conn/signal.
    router_only = by_mac["34:5A:60:D3:59:53"]
    assert router_only.name == "tower"
    assert router_only.source == "router"
    assert router_only.conn_type is None and router_only.signal is None
    # A hostname-less router-only lease still lands (name None).
    assert by_mac["02:0F:B5:EB:FB:FD"].name is None


def test_merge_router_leases_empty_passthrough() -> None:
    devices = [_ap_device("AA:BB:CC:00:00:01", "Phone")]
    assert network_client._merge_router_leases(devices, []) == devices
