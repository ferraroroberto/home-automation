"""Network client host-side probes."""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Any

import pytest

from src import network_client, network_host, network_router


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

    monkeypatch.setattr(network_host.sys, "platform", "win32")
    monkeypatch.setattr(network_host.subprocess, "run", fake_run)

    avg_ms, loss_pct = network_host._ping("192.0.2.1", count=2, timeout_s=1)

    assert avg_ms == 2.0
    assert loss_pct == 0.0
    assert calls[0]["cmd"] == ["ping", "-n", "2", "-w", "1000", "192.0.2.1"]
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW


def _capture_console_probe_kwargs(monkeypatch, platform: str) -> list[dict[str, Any]]:
    """Drive both host console probes and return the kwargs they spawned with."""
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(network_host.sys, "platform", platform)
    monkeypatch.setattr(network_host.subprocess, "run", fake_run)

    network_host._ping("192.0.2.1", count=1, timeout_s=1)
    network_host._run_quiet(["netsh", "wlan", "show", "interfaces"])
    return calls


def test_console_probes_pin_oem_decoding_on_windows(monkeypatch) -> None:
    """ping/netsh emit the OEM code page — never inherit text=True's locale.

    The webapp runs under PYTHONUTF8=1 (app/webapp/manager.py), so an ambient
    UTF-8 decode of an OEM-page reply silently yields empty/garbled stdout that
    the callers read as "the probe found nothing" rather than as a failure.
    """
    calls = _capture_console_probe_kwargs(monkeypatch, "win32")

    assert [c["encoding"] for c in calls] == ["oem", "oem"]
    assert [c["errors"] for c in calls] == ["replace", "replace"]


def test_console_probes_use_utf8_off_windows(monkeypatch) -> None:
    """POSIX has no ``oem`` codec (it would raise LookupError) — and no need."""
    calls = _capture_console_probe_kwargs(monkeypatch, "linux")

    assert [c["encoding"] for c in calls] == ["utf-8", "utf-8"]


def test_ping_reachable_true_when_ping_gets_a_reply(monkeypatch) -> None:
    monkeypatch.setattr(network_host, "_ping", lambda host, count=4, timeout_s=2: (3.5, 0.0))

    assert network_host._ping_reachable("192.0.2.1") is True


def test_ping_reachable_false_when_ping_gets_no_reply(monkeypatch) -> None:
    monkeypatch.setattr(network_host, "_ping", lambda host, count=4, timeout_s=2: (None, None))

    assert network_host._ping_reachable("192.0.2.1") is False


def test_ping_reachable_uses_a_single_fast_probe(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_ping(host, count=4, timeout_s=2):
        calls.append({"host": host, "count": count, "timeout_s": timeout_s})
        return None, None

    monkeypatch.setattr(network_host, "_ping", fake_ping)

    network_host._ping_reachable("192.0.2.1", timeout_s=1.0)

    assert calls == [{"host": "192.0.2.1", "count": 1, "timeout_s": 1.0}]


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
            ssid="TESTNET-A",
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
            ssid="TESTNET-B",
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
        return network_client.RouterHealth(reachable=True, authenticated=True), [], []

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


def test_resolve_ip_by_mac_finds_current_address(monkeypatch) -> None:
    async def fake_access_point():
        return object(), [
            _ap_device("AA:BB:CC:00:00:01", "phone", ip="192.168.0.11"),
            _ap_device("AA:BB:CC:F8:37:7F", "camera", ip="192.168.0.23"),
        ]

    monkeypatch.setattr(network_client, "fetch_access_point", fake_access_point)
    # Case-/separator-insensitive match returns the live IP.
    assert asyncio.run(network_client.resolve_ip_by_mac("aa:bb:cc:f8:37:7f")) == "192.168.0.23"
    # A MAC not in the table → None (caller leaves the device unreachable).
    assert asyncio.run(network_client.resolve_ip_by_mac("00:00:00:00:00:00")) is None


def test_resolve_ip_by_mac_returns_none_when_ap_unavailable(monkeypatch) -> None:
    async def boom():
        raise network_client.NetworkConfigError("no AP creds")

    async def no_router():
        raise network_client.NetworkConfigError("no router creds")

    monkeypatch.setattr(network_client, "fetch_access_point", boom)
    monkeypatch.setattr(network_client, "fetch_router", no_router)
    assert asyncio.run(network_client.resolve_ip_by_mac("aa:bb:cc:f8:37:7f")) is None


def test_resolve_ip_by_mac_finds_devices_on_the_routers_own_radio(monkeypatch) -> None:
    """Clients of the router's radios are invisible to the AP (issue #502), so an
    AP miss must fall through to the router or MAC pinning silently fails for
    exactly the devices the AP cannot see. The wlan-client rows carry no IP
    (issue #507) — the DHCPHostInfo lease table, which covers wireless clients
    too, is what resolves the address."""
    async def empty_ap():
        return object(), []

    async def fake_router():
        return (
            object(),
            [{"mac": "aa:bb:cc:00:00:37", "ip": "192.168.9.37", "hostname": "light-1"}],
            [{"mac": "aa:bb:cc:00:00:37", "hostname": "light-1", "ssid": "HomeNet",
              "conn_type": "5GHz", "signal": 74, "link_rate": 72}],
        )

    monkeypatch.setattr(network_client, "fetch_access_point", empty_ap)
    monkeypatch.setattr(network_client, "fetch_router", fake_router)
    assert asyncio.run(
        network_client.resolve_ip_by_mac("AA:BB:CC:00:00:37")
    ) == "192.168.9.37"


def test_resolve_ip_by_mac_falls_back_to_router_leases(monkeypatch) -> None:
    """A wired device appears in the lease table but on no radio at all."""
    async def empty_ap():
        return object(), []

    async def fake_router():
        return (
            object(),
            [{"mac": "aa:bb:cc:00:00:13", "ip": "192.168.9.13", "hostname": "tower"}],
            [],
        )

    monkeypatch.setattr(network_client, "fetch_access_point", empty_ap)
    monkeypatch.setattr(network_client, "fetch_router", fake_router)
    assert asyncio.run(
        network_client.resolve_ip_by_mac("AA:BB:CC:00:00:13")
    ) == "192.168.9.13"


def test_resolve_ip_by_mac_prefers_the_ap_and_skips_the_router_read(monkeypatch) -> None:
    """The AP is the cheaper read, so a hit there must not also fetch the router."""
    router_calls: list[int] = []

    async def ap_with_hit():
        return object(), [_ap_device("AA:BB:CC:00:00:01", "phone", ip="192.168.9.11")]

    async def fake_router():
        router_calls.append(1)
        return object(), [], []

    monkeypatch.setattr(network_client, "fetch_access_point", ap_with_hit)
    monkeypatch.setattr(network_client, "fetch_router", fake_router)
    assert asyncio.run(
        network_client.resolve_ip_by_mac("aa:bb:cc:00:00:01")
    ) == "192.168.9.11"
    assert router_calls == []


def test_merge_router_leases_fills_names_and_adds_router_only() -> None:
    devices = [
        _ap_device("AA:BB:CC:00:00:01", None),                 # no name → fill it
        _ap_device("AA:BB:CC:00:00:02", "Laptop"),             # has name → keep it
    ]
    leases = [
        {"mac": "aa:bb:cc:00:00:01", "ip": "192.168.0.58", "hostname": "SMA1930031140"},
        {"mac": "aa:bb:cc:00:00:02", "ip": "192.168.0.59", "hostname": "router-name"},
        {"mac": "aa:bb:cc:d3:59:53", "ip": "192.168.0.66", "hostname": "tower"},  # router-only
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
    router_only = by_mac["AA:BB:CC:D3:59:53"]
    assert router_only.name == "tower"
    assert router_only.source == "router"
    assert router_only.conn_type is None and router_only.signal is None
    # A hostname-less router-only lease still lands (name None).
    assert by_mac["02:0F:B5:EB:FB:FD"].name is None


def test_merge_router_leases_empty_passthrough() -> None:
    devices = [_ap_device("AA:BB:CC:00:00:01", "Phone")]
    assert network_client._merge_router_leases(devices, []) == devices


def test_merge_router_leases_dedups_repeated_mac_across_sources() -> None:
    """Two lease sources (DHCP table + homepage access-device table) disagree after
    a reboot clears the leases, so the same MAC arrives twice — it must update the
    one row, not append a second (issue #502)."""
    leases = [
        {"mac": "aa:bb:cc:00:00:09", "ip": "192.168.0.70", "hostname": None},
        {"mac": "AA:BB:CC:00:00:09", "ip": "192.168.0.70", "hostname": "desktop"},
    ]
    merged = network_client._merge_router_leases([], leases)

    assert len(merged) == 1
    # The second source's hostname fills the gap the first one left.
    assert merged[0].name == "desktop"
    # Corroboration between two *router* feeds is not AP corroboration.
    assert merged[0].source == "router"


# --- router-attached wireless clients (issue #502) --------------------------- #

def test_merge_router_wlan_clients_adds_devices_the_ap_cannot_see() -> None:
    """A client on the router's own radio reaches the inventory with its band,
    signal and link rate — before this it fell through both sources entirely."""
    clients = [{
        "mac": "AA:BB:CC:00:00:37", "hostname": "light-1",
        "ssid": "HomeNet", "conn_type": "5GHz", "signal": 74, "link_rate": 72,
    }]
    merged = network_client._merge_router_wlan_clients([], clients)

    assert len(merged) == 1
    dev = merged[0]
    assert dev.name == "light-1"
    assert dev.conn_type == "5GHz" and dev.is_wireless
    assert dev.signal == 74 and dev.link_rate == 72
    assert dev.ssid == "HomeNet"
    assert dev.source == "router"
    # The wlan feed carries no IP (issue #507) — the lease merge fills it later.
    assert dev.ip is None


def test_merge_router_wlan_clients_never_clobbers_ap_reported_values() -> None:
    """When both sources see a device the AP's own reading wins; the router only
    fills what the AP left blank, and the device becomes source="both"."""
    devices = [_ap_device("AA:BB:CC:00:00:01", "Laptop", signal=70, link_rate=866)]
    clients = [{
        "mac": "aa:bb:cc:00:00:01", "hostname": "other-name",
        "ssid": "OtherNet", "conn_type": "2.4GHz", "signal": 20, "link_rate": 65,
    }]
    merged = network_client._merge_router_wlan_clients(devices, clients)

    assert len(merged) == 1
    dev = merged[0]
    assert dev.name == "Laptop" and dev.signal == 70 and dev.link_rate == 866
    assert dev.conn_type == "5GHz" and dev.ssid == "HomeNet"
    assert dev.source == "both"


def test_merge_router_wlan_clients_overrides_the_aps_wired_uplink_artefact() -> None:
    """The AP sees a client of the *router's* radio as wired@100% because the
    traffic reaches it over its uplink port. That is topology, not a measurement,
    and merging an SSID onto it yields "wired ... ssid=<wireless net>". The router
    owns that link, so its band/signal/link-rate win."""
    devices = [_ap_device(
        "AA:BB:CC:00:00:14", "Desktop",
        conn_type="wired", signal=100, link_rate=None, ssid=None,
    )]
    clients = [{
        "mac": "aa:bb:cc:00:00:14", "hostname": "desktop",
        "ssid": "HomeNet", "conn_type": "5GHz", "signal": 76, "link_rate": 960,
    }]
    merged = network_client._merge_router_wlan_clients(devices, clients)

    assert len(merged) == 1
    dev = merged[0]
    assert dev.conn_type == "5GHz" and dev.is_wireless
    assert dev.signal == 76 and dev.link_rate == 960
    assert dev.ssid == "HomeNet"
    # The AP-reported display name still wins — only the link facts are replaced.
    assert dev.name == "Desktop"
    assert dev.source == "both"


def test_merge_router_wlan_clients_keeps_ap_wired_when_router_has_no_band() -> None:
    """A genuinely wired device the router also lists (no band) stays wired."""
    devices = [_ap_device(
        "AA:BB:CC:00:00:13", "Tower", conn_type="wired", signal=100, ssid=None,
    )]
    clients = [{"mac": "aa:bb:cc:00:00:13", "hostname": "tower",
                "ssid": None, "conn_type": None, "signal": None, "link_rate": None}]
    merged = network_client._merge_router_wlan_clients(devices, clients)

    assert merged[0].conn_type == "wired" and merged[0].signal == 100


def test_merge_router_wlan_clients_empty_passthrough() -> None:
    devices = [_ap_device("AA:BB:CC:00:00:01", "Phone")]
    assert network_client._merge_router_wlan_clients(devices, []) == devices


@pytest.mark.parametrize(("alias", "expected"), [
    ("DEV.WIFI.AP1", "2.4GHz"),
    ("DEV.WIFI.AP4", "2.4GHz"),
    ("DEV.WIFI.AP5", "5GHz"),
    ("DEV.WIFI.AP8", "5GHz"),
    ("DEV.WIFI.AP9", None),   # outside the unit's VAP layout
    ("nonsense", None),
    (None, None),
])
def test_vap_band_maps_vap_alias_to_radio_band(alias, expected) -> None:
    assert network_router._vap_band(alias) == expected


@pytest.mark.parametrize(("rssi", "expected"), [
    ("-50", 100),   # strong link clamps at 100, not 200
    ("-63", 74),
    ("-79", 42),
    ("-100", 0),
    ("-120", 0),    # clamps at the bottom rather than going negative
    ("0", None),    # firmware placeholder for "no reading yet"
    ("", None),
    (None, None),
])
def test_rssi_to_pct_converts_dbm_to_the_percent_scale(rssi, expected) -> None:
    """The AP reports percent and the router reports dBm; they have to land on one
    scale or the weak-signal alert threshold means two different things."""
    assert network_router._rssi_to_pct(rssi) == expected


@pytest.mark.parametrize(("rate", "expected"), [
    ("960000", 960), ("72000", 72), ("0", None), ("", None), (None, None),
])
def test_kbps_to_mbps(rate, expected) -> None:
    assert network_router._kbps_to_mbps(rate) == expected


# --------------------------------------------------------------------------- #
# AP rediscovery (issue #150)                                                  #
# --------------------------------------------------------------------------- #

def _make_fetch_network_state_fakes(
    *,
    ap_reachable: bool,
    router_leases: list[dict],
    ap_mac_env: str = "",
    rediscovered_ap_reachable: bool = True,
    rediscovered_devices: "list[network_client.NetDevice] | None" = None,
):
    """Return a dict of monkeypatch targets for fetch_network_state rediscovery tests."""

    async def fake_internet(include_speedtest: bool = False):
        return network_client.InternetHealth(online=True)

    async def fake_access_point():
        if ap_reachable:
            return network_client.AccessPointHealth(reachable=True), []
        return network_client.AccessPointHealth(reachable=False, error="Connection timed out"), []

    async def fake_router():
        return network_client.RouterHealth(reachable=True, authenticated=True), router_leases, []

    async def fake_wifi():
        return network_client.WifiDiagnostics(available=False)

    def fake_fetch_ap_sync(host_override=None):
        if rediscovered_ap_reachable:
            devs = rediscovered_devices or []
            return network_client.AccessPointHealth(reachable=True, device_count=len(devs)), devs
        return network_client.AccessPointHealth(reachable=False, error="probe failed"), []

    return {
        "fetch_internet_health": fake_internet,
        "fetch_access_point": fake_access_point,
        "fetch_router": fake_router,
        "fetch_wifi_diagnostics": fake_wifi,
        "_fetch_ap_sync": fake_fetch_ap_sync,
        "_ap_mac_env": ap_mac_env,
    }


def test_ap_rediscovery_success(monkeypatch) -> None:
    """Stale NETWORK_AP_HOST fails; MAC found in router leases; probe succeeds."""
    leases = [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.55", "hostname": "R9000"}]
    fakes = _make_fetch_network_state_fakes(
        ap_reachable=False,
        router_leases=leases,
        ap_mac_env="aa:bb:cc:dd:ee:ff",
        rediscovered_ap_reachable=True,
    )

    # Patch _ap_mac directly: it calls load_dotenv(override=True), which would
    # clobber a monkeypatch.setenv from the real .env when NETWORK_AP_MAC is set.
    monkeypatch.setattr(network_client, "_ap_mac", lambda: fakes["_ap_mac_env"])
    monkeypatch.setattr(network_client, "fetch_internet_health", fakes["fetch_internet_health"])
    monkeypatch.setattr(network_client, "fetch_access_point", fakes["fetch_access_point"])
    monkeypatch.setattr(network_client, "fetch_router", fakes["fetch_router"])
    monkeypatch.setattr(network_client, "fetch_wifi_diagnostics", fakes["fetch_wifi_diagnostics"])
    monkeypatch.setattr(network_client, "_fetch_ap_sync", fakes["_fetch_ap_sync"])

    state = asyncio.run(network_client.fetch_network_state())

    assert state.access_point.reachable is True
    assert not any("Access point unreachable" in a for a in state.alerts)


def test_ap_rediscovery_failure_mac_not_in_leases(monkeypatch) -> None:
    """Stale host fails; MAC absent from router leases → unreachable (same as today)."""
    leases = [{"mac": "11:22:33:44:55:66", "ip": "192.168.0.10", "hostname": "other"}]
    fakes = _make_fetch_network_state_fakes(
        ap_reachable=False,
        router_leases=leases,
        ap_mac_env="aa:bb:cc:dd:ee:ff",  # MAC not in leases
    )

    # Patch _ap_mac directly: it calls load_dotenv(override=True), which would
    # clobber a monkeypatch.setenv from the real .env when NETWORK_AP_MAC is set.
    monkeypatch.setattr(network_client, "_ap_mac", lambda: fakes["_ap_mac_env"])
    monkeypatch.setattr(network_client, "fetch_internet_health", fakes["fetch_internet_health"])
    monkeypatch.setattr(network_client, "fetch_access_point", fakes["fetch_access_point"])
    monkeypatch.setattr(network_client, "fetch_router", fakes["fetch_router"])
    monkeypatch.setattr(network_client, "fetch_wifi_diagnostics", fakes["fetch_wifi_diagnostics"])

    state = asyncio.run(network_client.fetch_network_state())

    assert state.access_point.reachable is False
    assert any("Access point unreachable" in a for a in state.alerts)


# --------------------------------------------------------------------------- #
# No-link device ping probe (issue #552)                                      #
# --------------------------------------------------------------------------- #
# _ping_reachable runs inside a real asyncio.to_thread() worker thread below —
# it's a plain, fast, monkeypatched function (never a real subprocess/ping in
# these tests), so letting the real thread pool run it is simpler and safer
# than faking asyncio internals, and it exercises the real bounded-timeout path.
def _nolink_device(mac: str, ip: str | None = "192.168.0.50", **kw) -> "network_client.NetDevice":
    base = dict(ip=ip, name="Lease only", conn_type=None, signal=None,
                link_rate=None, ssid=None, source="router")
    base.update(kw)
    return network_client.NetDevice(mac=mac, **base)


@pytest.fixture(autouse=True)
def _clear_ping_probe_cache():
    network_client._ping_probe_cache.clear()
    yield
    network_client._ping_probe_cache.clear()


def test_probe_nolink_devices_only_probes_signalless_non_wired_devices_with_ip(monkeypatch) -> None:
    calls: list[str] = []

    def fake_reachable(ip):
        calls.append(ip)
        return True

    monkeypatch.setattr(network_client, "_ping_reachable", fake_reachable)

    nolink = _nolink_device("AA:AA:AA:AA:AA:01")
    wired = _ap_device("AA:AA:AA:AA:AA:02", "Printer", conn_type="wired", signal=None)
    has_signal = _ap_device("AA:AA:AA:AA:AA:03", "Laptop", signal=70)
    no_ip = _nolink_device("AA:AA:AA:AA:AA:04", ip=None)

    devices = [nolink, wired, has_signal, no_ip]
    updated = asyncio.run(network_client._probe_nolink_devices(devices))

    assert calls == ["192.168.0.50"]
    by_mac = {d.mac: d for d in updated}
    assert by_mac["AA:AA:AA:AA:AA:01"].ping_reachable is True
    assert by_mac["AA:AA:AA:AA:AA:02"].ping_reachable is None
    assert by_mac["AA:AA:AA:AA:AA:03"].ping_reachable is None
    assert by_mac["AA:AA:AA:AA:AA:04"].ping_reachable is None


def test_probe_nolink_devices_records_unreachable_result(monkeypatch) -> None:
    monkeypatch.setattr(network_client, "_ping_reachable", lambda ip: False)

    nolink = _nolink_device("AA:AA:AA:AA:AA:05")
    updated = asyncio.run(network_client._probe_nolink_devices([nolink]))

    assert updated[0].ping_reachable is False


def test_probe_nolink_devices_returns_unchanged_list_when_nothing_needs_probing(monkeypatch) -> None:
    called = False

    def fake_reachable(ip):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(network_client, "_ping_reachable", fake_reachable)

    wired = _ap_device("AA:AA:AA:AA:AA:06", "Printer", conn_type="wired", signal=None)
    devices = [wired]
    updated = asyncio.run(network_client._probe_nolink_devices(devices))

    assert called is False
    assert updated == devices


def test_probe_nolink_devices_reuses_cached_result_within_ttl_then_reprobes_after(monkeypatch) -> None:
    call_count = 0

    def fake_reachable(ip):
        nonlocal call_count
        call_count += 1
        return True

    monkeypatch.setattr(network_client, "_ping_reachable", fake_reachable)

    device = _nolink_device("AA:AA:AA:AA:AA:07")
    asyncio.run(network_client._probe_nolink_devices([device]))
    assert call_count == 1

    # Still fresh — served from cache, no second probe.
    asyncio.run(network_client._probe_nolink_devices([device]))
    assert call_count == 1

    # Backdate the cached entry's timestamp directly (not the real clock, which
    # asyncio's own scheduling/timeout machinery also reads) past the TTL —
    # re-probed.
    key = (device.mac, device.ip)
    checked_at, reachable = network_client._ping_probe_cache[key]
    network_client._ping_probe_cache[key] = (
        checked_at - network_client._PING_PROBE_CACHE_TTL_S - 1, reachable,
    )
    asyncio.run(network_client._probe_nolink_devices([device]))
    assert call_count == 2


def test_probe_nolink_devices_bounded_by_timeout_leaves_device_unprobed(monkeypatch) -> None:
    def slow_reachable(ip):
        time.sleep(0.2)
        return True

    monkeypatch.setattr(network_client, "_PING_PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(network_client, "_ping_reachable", slow_reachable)

    device = _nolink_device("AA:AA:AA:AA:AA:08")
    updated = asyncio.run(network_client._probe_nolink_devices([device]))

    assert updated[0].ping_reachable is None


def test_fetch_network_state_promotes_a_ping_reachable_nolink_device(monkeypatch) -> None:
    async def fake_internet_health(include_speedtest: bool = False):
        return network_client.InternetHealth(online=True, external_ms=12)

    async def fake_access_point():
        return network_client.AccessPointHealth(reachable=True), [
            _nolink_device("AA:AA:AA:AA:AA:09", ip="192.168.0.60")
        ]

    async def fake_router():
        return network_client.RouterHealth(reachable=True, authenticated=True), [], []

    async def fake_wifi():
        return network_client.WifiDiagnostics(available=False)

    monkeypatch.setattr(network_client, "fetch_internet_health", fake_internet_health)
    monkeypatch.setattr(network_client, "fetch_access_point", fake_access_point)
    monkeypatch.setattr(network_client, "fetch_router", fake_router)
    monkeypatch.setattr(network_client, "fetch_wifi_diagnostics", fake_wifi)
    monkeypatch.setattr(network_client, "_ping_reachable", lambda ip: True)

    state = asyncio.run(network_client.fetch_network_state())

    assert len(state.devices) == 1
    assert state.devices[0].ping_reachable is True
