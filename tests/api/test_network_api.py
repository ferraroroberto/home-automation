"""API smoke for the network/Wi-Fi/router visibility + control endpoints.

``GET /api/network`` flattens LAN/Wi-Fi/router state, merges the hidden and
display-name overrides, and tracks online/offline + "new" history; the
reboot and rename routes drive the same core. Core fetch and router I/O are
monkeypatched throughout — never a real LAN scan or router login.
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
    WifiBssid,
    WifiChannelInsight,
    WifiChannelScore,
    WifiDiagnostics,
)


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
    # The response carries no ``alerts`` key — the UI strip that read it is gone
    # and a weak client is highlighted on its own row instead (issue #632).
    assert "alerts" not in body

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
    """``GET /api/network`` derives the online/offline rows + the important flag.

    A device present in one read then absent from the next is synthesised as an
    ``online=false`` row carrying its last-known identity and its ``important``
    flag — the per-row surface that replaced the removed alert strip (#632).
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

    # First read seeds the registry: both online, and a seed is never badged new.
    body = client.get("/api/network").json()
    by_mac = {d["mac"]: d for d in body["devices"]}
    assert by_mac["5C:CF:7F:AA:BB:CC"]["online"] is True
    assert by_mac["B8:27:EB:11:22:33"]["online"] is True
    assert by_mac["5C:CF:7F:AA:BB:CC"]["is_new"] is False
    assert by_mac["B8:27:EB:11:22:33"]["is_new"] is False

    # Mark the wired host important (lower-case MAC normalises to the store key).
    resp = client.post(
        "/api/network/devices/b8:27:eb:11:22:33/important", json={"important": True}
    )
    assert resp.status_code == 200
    assert resp.json() == {"mac": "B8:27:EB:11:22:33", "important": True}

    # Next read: the important device drops off → a synthesised offline row.
    holder["state"] = NetworkState(devices=(dev_a,), **base)
    body = client.get("/api/network").json()
    by_mac = {d["mac"]: d for d in body["devices"]}
    assert by_mac["5C:CF:7F:AA:BB:CC"]["online"] is True
    offline = by_mac["B8:27:EB:11:22:33"]
    assert offline["online"] is False
    assert offline["important"] is True
    assert offline["source"] == "history"
    assert offline["ip"] == "192.168.0.10"  # last-known IP retained
