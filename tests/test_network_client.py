"""Network client host-side probes."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
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
    exactly the devices the AP cannot see."""
    async def empty_ap():
        return object(), []

    async def fake_router():
        return (
            object(),
            [],
            [{"mac": "aa:bb:cc:00:00:37", "ip": "192.168.9.37"}],
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
        "mac": "AA:BB:CC:00:00:37", "ip": "192.168.0.37", "hostname": "light-1",
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


def test_merge_router_wlan_clients_never_clobbers_ap_reported_values() -> None:
    """When both sources see a device the AP's own reading wins; the router only
    fills what the AP left blank, and the device becomes source="both"."""
    devices = [_ap_device("AA:BB:CC:00:00:01", "Laptop", signal=70, link_rate=866)]
    clients = [{
        "mac": "aa:bb:cc:00:00:01", "ip": "192.168.0.10", "hostname": "other-name",
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
        "mac": "aa:bb:cc:00:00:14", "ip": "192.168.0.14", "hostname": "desktop",
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
    clients = [{"mac": "aa:bb:cc:00:00:13", "ip": "192.168.0.13", "hostname": "tower",
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


def _instances_xml(rows: list[dict]) -> str:
    body = "".join(
        "<Instance>"
        + "".join(f"<ParaName>{k}</ParaName><ParaValue>{v}</ParaValue>"
                  for k, v in row.items())
        + "</Instance>"
        for row in rows
    )
    return f"<ajax_response_xml_root>{body}</ajax_response_xml_root>"


def test_read_wlan_clients_parses_clients_and_skips_ssid_rows(monkeypatch) -> None:
    """The feed interleaves per-VAP SSID descriptor rows (no MACAddress) with real
    client rows; only the clients come back, normalised onto NetDevice's units."""
    xml = _instances_xml([
        {"_InstID": "DEV.WIFI.AP5.AD1", "AliasName": "DEV.WIFI.AP5",
         "MACAddress": "AA:BB:CC:00:00:37", "IPAddress": "192.168.0.37",
         "HostName": "light-1", "SSIDName": "HomeNet", "RSSI": "-63",
         "TxRate": "72000"},
        # Associated but not yet leased an address -> ip is unknown, not 0.0.0.0.
        {"_InstID": "DEV.WIFI.AP1.AD1", "AliasName": "DEV.WIFI.AP1",
         "MACAddress": "AA:BB:CC:00:00:16", "IPAddress": "0.0.0.0",
         "HostName": "", "SSIDName": "HomeNet", "RSSI": "-57", "TxRate": "65000"},
        # Per-VAP descriptor row: no MACAddress, must not become a device.
        {"_InstID": "DEV.WIFI.AP5", "Alias": "SSID5", "ESSID": "HomeNet"},
    ])
    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(client, "_menu_view", lambda tag, timeout=10: "")
    monkeypatch.setattr(
        client, "_session",
        SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(text=xml)),
    )

    rows = client.read_wlan_clients()

    assert len(rows) == 2
    first, second = rows
    assert first["mac"] == "AA:BB:CC:00:00:37"
    assert first["ip"] == "192.168.0.37"
    assert first["hostname"] == "light-1"
    assert first["conn_type"] == "5GHz"
    assert first["signal"] == 74 and first["link_rate"] == 72
    assert second["conn_type"] == "2.4GHz"
    assert second["ip"] is None       # 0.0.0.0 is "unknown", not an address
    assert second["hostname"] is None  # empty HostName normalises to None


def test_read_wlan_clients_raises_on_session_rejection(monkeypatch) -> None:
    """A page-gate miss returns SessionTimeout with HTTP 200 — it must raise, not
    be parsed as an empty client list (which would read as "nothing attached")."""
    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(client, "_menu_view", lambda tag, timeout=10: "")
    monkeypatch.setattr(
        client, "_session",
        SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(text="SessionTimeout")),
    )
    with pytest.raises(network_client.NetworkCommandError):
        client.read_wlan_clients()


def test_read_accessdev_table_returns_lease_shaped_rows(monkeypatch) -> None:
    """The homepage table is merged through _merge_router_leases, so it has to come
    back in exactly the {mac, ip, hostname} shape that function consumes."""
    xml = _instances_xml([
        {"HostName": "desktop", "IPAddress": "192.168.0.51",
         "MACAddress": "AA:BB:CC:00:00:51", "LastConnection": "2026-01-01 07:16:29"},
        {"HostName": "", "IPAddress": "192.168.0.52",
         "MACAddress": "AA:BB:CC:00:00:52", "LastConnection": ""},
    ])
    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(
        client, "_session",
        SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(text=xml)),
    )

    rows = client.read_accessdev_table()

    assert rows == [
        {"mac": "AA:BB:CC:00:00:51", "ip": "192.168.0.51", "hostname": "desktop"},
        {"mac": "AA:BB:CC:00:00:52", "ip": "192.168.0.52", "hostname": None},
    ]


# --- DHCP-binding write-back: full-table cap handling (issue #176) ----------- #
# The F6600P caps its static-binding table; the (N+1)th create returns this exact
# IF_ERRORID -12 body (spaces as &#32;). We must classify it as "table full", not a
# generic reject, and never as success — note the decoy <IF_ERRORPARAM>SUCC.
_FULL_TABLE_XML = (
    "<ajax_response_xml_root><INSTIDENTITY></INSTIDENTITY>"
    "<IF_ERRORID>-12</IF_ERRORID><IF_ERRORTYPE>1</IF_ERRORTYPE>"
    "<IF_ERRORSTR>The&#32;number&#32;of&#32;entries&#32;has&#32;reached&#32;the&#32;"
    "maximum&#32;limit,&#32;please&#32;delete&#32;some&#32;entries&#32;and&#32;input&#32;"
    "again.&#32;</IF_ERRORSTR><IF_ERRORPARAM>SUCC</IF_ERRORPARAM><_InstID></_InstID>"
    "</ajax_response_xml_root>"
)


def test_check_binding_result_flags_full_table() -> None:
    with pytest.raises(network_client.DhcpBindingTableFull):
        network_client.RouterClient._check_binding_result(_FULL_TABLE_XML, "write")


def test_check_binding_result_success_and_generic_reject() -> None:
    assert network_client.RouterClient._check_binding_result(
        "<IF_ERRORSTR>SUCC</IF_ERRORSTR>", "write"
    ) is True
    with pytest.raises(network_client.NetworkCommandError) as exc:
        network_client.RouterClient._check_binding_result(
            "<IF_ERRORSTR>Invalid&#32;IP&#32;address</IF_ERRORSTR>", "write"
        )
    # A non-cap reject is generic and its message is HTML-unescaped (readable).
    assert not isinstance(exc.value, network_client.DhcpBindingTableFull)
    assert "Invalid IP address" in str(exc.value)


class _FakeFullRouter:
    """A RouterClient stand-in whose binding table is already full."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.written: list[tuple[str, str, bool]] = []
        # Exactly DHCP_BIND_MAX existing rows → zero free slots.
        self._existing = [
            {
                "name": f"d{i}",
                "mac": f"00:00:00:00:00:{i:02x}",
                "ip": f"192.168.0.{i}",
                "inst_id": f"B{i}",
            }
            for i in range(network_client.DHCP_BIND_MAX)
        ]

    def login(self) -> bool:
        return True

    def read_dhcp_bindings(self, timeout: int = 10) -> list:
        return list(self._existing)

    def _write_binding(self, name, mac, ip, prior, timeout: int = 10) -> bool:
        self.written.append((mac, ip, bool(prior)))
        return True


def test_apply_skips_creates_when_table_full_but_keeps_replaces(monkeypatch) -> None:
    """A full table skips new reservations without hammering the router, yet a
    slot-neutral replace (MAC already bound) still applies (issue #176)."""
    fake = _FakeFullRouter()
    monkeypatch.setattr(network_client, "_router_creds", lambda: ("h", "u", "p"))
    monkeypatch.setattr(network_client, "RouterClient", lambda *a, **k: fake)

    rows = [
        {"name": "new cam", "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.0.50"},   # create
        {"name": "moved", "mac": "00:00:00:00:00:00", "ip": "192.168.0.200"},    # replace
    ]
    results = network_client._apply_dhcp_bindings_sync(rows)
    by_mac = {r["mac"]: r for r in results}

    # The create can't fit → recorded as skipped, with no write attempted for it.
    assert by_mac["AA:BB:CC:DD:EE:FF"]["ok"] is False
    assert by_mac["AA:BB:CC:DD:EE:FF"]["skipped"] is True
    assert all(w[0] != "AA:BB:CC:DD:EE:FF" for w in fake.written)

    # The replace is slot-neutral → still written (delete prior + add).
    assert by_mac["00:00:00:00:00:00"]["ok"] is True
    assert ("00:00:00:00:00:00", "192.168.0.200", True) in fake.written


def test_write_binding_wraps_create_failure_as_reservation_lost_after_delete(
    monkeypatch,
) -> None:
    """A replace's create failing *after* the prior delete already succeeded must
    surface as DhcpReservationLost, not a generic reject (issue #347) — the router
    now has no static reservation at all for this MAC."""
    client = network_client.RouterClient("h", "u", "p")
    deleted: list[str] = []
    monkeypatch.setattr(
        client, "_delete_dhcp_binding",
        lambda inst_id, timeout=10: deleted.append(inst_id) or True,
    )
    monkeypatch.setattr(
        client, "_binding_post",
        lambda body, timeout=10: "<IF_ERRORSTR>Invalid&#32;IP&#32;address</IF_ERRORSTR>",
    )

    prior = {"name": "old", "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.0.1", "inst_id": "B1"}
    with pytest.raises(network_client.DhcpReservationLost) as exc:
        client._write_binding("new", "AA:BB:CC:DD:EE:FF", "192.168.0.2", prior)

    assert deleted == ["B1"]  # the prior binding really was deleted first
    assert "AA:BB:CC:DD:EE:FF" in str(exc.value)


def test_write_binding_reraises_plain_error_when_nothing_was_deleted(monkeypatch) -> None:
    """A genuinely new reservation's failed create has nothing to lose — it raises
    the plain error, not DhcpReservationLost (no prior binding was ever deleted)."""
    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(
        client, "_binding_post",
        lambda body, timeout=10: "<IF_ERRORSTR>Invalid&#32;IP&#32;address</IF_ERRORSTR>",
    )

    with pytest.raises(network_client.NetworkCommandError) as exc:
        client._write_binding("new", "AA:BB:CC:DD:EE:FF", "192.168.0.2", None)
    assert not isinstance(exc.value, network_client.DhcpReservationLost)


class _FakeReservationLostRouter:
    """A RouterClient stand-in whose replace create fails after its delete succeeds."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self._existing = [
            {"name": "old", "mac": "00:00:00:00:00:01", "ip": "192.168.0.1", "inst_id": "B1"},
        ]

    def login(self) -> bool:
        return True

    def read_dhcp_bindings(self, timeout: int = 10) -> list:
        return list(self._existing)

    def _write_binding(self, name, mac, ip, prior, timeout: int = 10) -> bool:
        raise network_client.DhcpReservationLost(
            f"router lost its DHCP reservation for {mac}"
        )


def test_apply_bindings_records_reservation_lost_row(monkeypatch) -> None:
    """A DhcpReservationLost from _write_binding is recorded distinctly in the
    per-row result, not as a plain failed row (issue #347)."""
    fake = _FakeReservationLostRouter()
    monkeypatch.setattr(network_client, "_router_creds", lambda: ("h", "u", "p"))
    monkeypatch.setattr(network_client, "RouterClient", lambda *a, **k: fake)

    rows = [{"name": "moved", "mac": "00:00:00:00:00:01", "ip": "192.168.0.200"}]
    results = network_client._apply_dhcp_bindings_sync(rows)

    assert len(results) == 1
    row = results[0]
    assert row["ok"] is False
    assert row["reservation_lost"] is True
    assert "lost its DHCP reservation" in row["error"]


def test_delete_dhcp_binding_sync_logs_in_and_deletes(monkeypatch) -> None:
    """``delete_dhcp_binding`` logs in once and removes the row by its inst_id (#176)."""
    captured: dict = {}

    class _Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def login(self) -> bool:
            return True

        def _delete_dhcp_binding(self, inst_id: str, timeout: int = 10) -> bool:
            captured["inst_id"] = inst_id
            return True

    monkeypatch.setattr(network_client, "_router_creds", lambda: ("h", "u", "p"))
    monkeypatch.setattr(network_client, "RouterClient", _Fake)

    assert network_client._delete_dhcp_binding_sync("DEV.V4DP.Sr.Pl1.Bd2") is True
    assert captured["inst_id"] == "DEV.V4DP.Sr.Pl1.Bd2"


def test_apply_changes_deletes_then_adds_on_one_session(monkeypatch) -> None:
    """The staged batch deletes first, then adds — on one login (issue #176 redesign).

    Deleting before the add pass frees a slot, and the add pass re-reads the table,
    so a create that wouldn't have fit now does. Each op is tagged for the caller.
    """

    class _Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.deleted: list[str] = []
            self.written: list[tuple[str, str]] = []
            # One existing row that gets deleted, freeing the only slot for the add.
            self._existing = [
                {"name": "x", "mac": "00:00:00:00:00:01", "ip": "192.168.0.1", "inst_id": "B1"}
            ]

        def login(self) -> bool:
            return True

        def _delete_dhcp_binding(self, inst_id: str, timeout: int = 10) -> bool:
            self.deleted.append(inst_id)
            return True

        def read_dhcp_bindings(self, timeout: int = 10) -> list:
            return list(self._existing)

        def _write_binding(self, name, mac, ip, prior, timeout: int = 10) -> bool:
            self.written.append((mac, ip))
            return True

    fake = _Fake()
    monkeypatch.setattr(network_client, "_router_creds", lambda: ("h", "u", "p"))
    monkeypatch.setattr(network_client, "RouterClient", lambda *a, **k: fake)

    results = network_client._apply_dhcp_changes_sync(
        ["B1"], [{"name": "cam", "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.0.50"}]
    )
    assert fake.deleted == ["B1"]
    assert ("AA:BB:CC:DD:EE:FF", "192.168.0.50") in fake.written
    assert {r["op"] for r in results} == {"remove", "add"}
    assert all(r["ok"] for r in results)


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
