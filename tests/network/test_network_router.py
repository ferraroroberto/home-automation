"""Router-side network client: WLAN/DHCP reads, binding writes, signed POSTs.

Every router exchange is faked; nothing here opens a session to real hardware.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src import network_client, network_router


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
    client rows; only the clients come back, normalised onto NetDevice's units.
    The feed's IPAddress is 0.0.0.0/empty for every client, so no ``ip`` field is
    emitted at all (issue #507) — a phantom ``ip=None`` read as data and misled a
    live debugging session."""
    xml = _instances_xml([
        {"_InstID": "DEV.WIFI.AP5.AD1", "AliasName": "DEV.WIFI.AP5",
         "MACAddress": "AA:BB:CC:00:00:37", "IPAddress": "0.0.0.0",
         "HostName": "light-1", "SSIDName": "HomeNet", "RSSI": "-63",
         "TxRate": "72000"},
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
    assert first["hostname"] == "light-1"
    assert first["conn_type"] == "5GHz"
    assert first["signal"] == 74 and first["link_rate"] == 72
    assert second["conn_type"] == "2.4GHz"
    assert second["hostname"] is None  # empty HostName normalises to None
    # No phantom ip field — the feed has no usable IP to report (issue #507).
    assert all("ip" not in row for row in rows)


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


def test_read_dhcp_leases_parses_dhcphostinfo_wired_and_wireless(monkeypatch) -> None:
    """The DHCPHostInfo feed (issue #507) returns the FULL lease table — wired and
    SSID-attached rows — with MACAddr/IPAddr key names (not MACAddress/IPAddress).
    A wireless client must come back with its IP; the old accessdev_landevs feed
    missed wireless clients entirely."""
    xml = _instances_xml([
        {"_InstID": "DEV.LD1.HOST1", "HostName": "desktop", "MACAddr": "AA:BB:CC:00:00:51",
         "IPAddr": "192.168.0.51", "PhyPortName": "LAN4", "ExpiredTime": "85000"},
        # Wireless (router-radio) client — present, with its IP.
        {"_InstID": "DEV.LD1.HOST2", "HostName": "light-1", "MACAddr": "AA:BB:CC:00:00:37",
         "IPAddr": "192.168.0.37", "PhyPortName": "SSID5", "ExpiredTime": "42000"},
        # Empty HostName normalises to None; SSID1 row is 2.4 GHz wireless.
        {"_InstID": "DEV.LD1.HOST3", "HostName": "", "MACAddr": "AA:BB:CC:00:00:16",
         "IPAddr": "192.168.0.16", "PhyPortName": "SSID1", "ExpiredTime": "42000"},
        # A row with no MACAddr is unusable for the MAC-keyed merge — skipped.
        {"_InstID": "DEV.LD1.HOST4", "HostName": "ghost", "IPAddr": "192.168.0.99"},
    ])
    gates: list[str] = []
    urls: list[str] = []

    def fake_get(url, *a, **kw):
        urls.append(url)
        return SimpleNamespace(text=xml)

    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(client, "_menu_view", lambda tag, timeout=10: gates.append(tag) or "")
    monkeypatch.setattr(client, "_session", SimpleNamespace(get=fake_get))

    rows = client.read_dhcp_leases()

    # Page-gated behind the lanMgrIpv4 LAN page, reading the DHCPHostInfo feed.
    assert gates == ["lanMgrIpv4"]
    assert "Localnet_LanMgrIpv4_DHCPHostInfo_lua.lua" in urls[0]
    assert rows == [
        {"mac": "AA:BB:CC:00:00:51", "ip": "192.168.0.51", "hostname": "desktop"},
        {"mac": "AA:BB:CC:00:00:37", "ip": "192.168.0.37", "hostname": "light-1"},
        {"mac": "AA:BB:CC:00:00:16", "ip": "192.168.0.16", "hostname": None},
    ]


def test_read_dhcp_leases_raises_on_session_rejection(monkeypatch) -> None:
    """A page-gate miss returns SessionTimeout with HTTP 200 — it must raise, not
    parse as an empty lease table (which would read as "no devices leased")."""
    client = network_client.RouterClient("h", "u", "p")
    monkeypatch.setattr(client, "_menu_view", lambda tag, timeout=10: "")
    monkeypatch.setattr(
        client, "_session",
        SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(text="SessionTimeout")),
    )
    with pytest.raises(network_client.NetworkCommandError):
        client.read_dhcp_leases()


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


def test_apply_skips_creates_once_its_own_batch_fills_the_table(monkeypatch) -> None:
    """One free slot, two creates: the first lands, the second is skipped as
    "table is full" — never sent for the router to reject (audit #916)."""
    fake = _FakeFullRouter()
    fake._existing.pop()  # exactly one free slot
    monkeypatch.setattr(network_client, "_router_creds", lambda: ("h", "u", "p"))
    monkeypatch.setattr(network_client, "RouterClient", lambda *a, **k: fake)

    rows = [
        {"name": "first", "mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.51"},
        {"name": "second", "mac": "AA:BB:CC:DD:EE:02", "ip": "192.168.0.52"},
        {"name": "moved", "mac": "00:00:00:00:00:00", "ip": "192.168.0.200"},  # replace
    ]
    by_mac = {r["mac"]: r for r in network_client._apply_dhcp_bindings_sync(rows)}

    assert by_mac["AA:BB:CC:DD:EE:01"]["ok"] is True
    assert by_mac["AA:BB:CC:DD:EE:02"]["ok"] is False
    assert by_mac["AA:BB:CC:DD:EE:02"]["skipped"] is True
    assert "table is full" in by_mac["AA:BB:CC:DD:EE:02"]["error"]
    assert all(w[0] != "AA:BB:CC:DD:EE:02" for w in fake.written)
    # The slot-neutral replace still applies after the table filled.
    assert by_mac["00:00:00:00:00:00"]["ok"] is True


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


# ---- integrity-signed router POSTs (binding writes + reboot), fakes only ---- #

_HOME_HTML = (
    'pubKey = "-----BEGIN PUBLIC KEY-----\nKEY\n-----END PUBLIC KEY-----"; '
    r'_sessionTmpToken = "\x48\x4f\x4d\x45";'
)
_PAGE_HTML = r'_sessionTmpToken = "\x52\x4f\x54";'  # the rotated token: "ROT"


class _FakeRouterSession:
    """Records the signed-POST sequence; never touches a network."""

    def __init__(self, *, post_text: str = "<IF_ERRORSTR>SUCC</IF_ERRORSTR>",
                 post_exc: Exception | None = None, get_exc: Exception | None = None,
                 page_html: str = _PAGE_HTML, home_html: str = _HOME_HTML) -> None:
        self.post_text, self.post_exc, self.get_exc = post_text, post_exc, get_exc
        self.page_html, self.home_html = page_html, home_html
        self.gets: list[str] = []
        self.posts: list[dict] = []

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> SimpleNamespace:
        if self.get_exc is not None:
            raise self.get_exc
        self.gets.append(url)
        return SimpleNamespace(text=self.page_html if "menuView" in url else self.home_html)

    def post(self, url: str, data: str, headers: dict, timeout: Any = None) -> SimpleNamespace:
        self.posts.append({"url": url, "data": data, "headers": headers})
        if self.post_exc is not None:
            raise self.post_exc
        return SimpleNamespace(text=self.post_text)


def _signed_client(monkeypatch, session: _FakeRouterSession) -> Any:
    client = network_router.RouterClient("h", "u", "p")
    client._session = session
    monkeypatch.setattr(network_router, "_asy_encode", lambda pem, src: f"sig[{pem}|{src}]")
    return client


def _expected_check(body: str) -> str:
    import hashlib

    pem = "-----BEGIN PUBLIC KEY-----\nKEY\n-----END PUBLIC KEY-----"
    return f"sig[{pem}|{hashlib.sha256(body.encode()).hexdigest()}]"


def test_binding_post_signs_body_with_the_rotated_token(monkeypatch) -> None:
    session = _FakeRouterSession(post_text="<ok/>")
    client = _signed_client(monkeypatch, session)

    assert client._binding_post("IF_ACTION=Apply&x=1") == "<ok/>"

    assert session.gets == ["http://h/", "http://h/?_type=menuView&_tag=lanMgrIpv4"]
    (post,) = session.posts
    assert post["url"] == "http://h" + client._DHCP_BIND_FEED
    assert post["data"] == "IF_ACTION=Apply&x=1&_sessionTOKEN=ROT"
    assert post["headers"]["Referer"] == "http://h/?_type=menuView&_tag=lanMgrIpv4"
    assert post["headers"]["Check"] == _expected_check(post["data"])


def test_binding_post_surfaces_a_transport_error(monkeypatch) -> None:
    import requests

    session = _FakeRouterSession(post_exc=requests.ConnectionError("reset"))
    client = _signed_client(monkeypatch, session)
    with pytest.raises(requests.RequestException):
        client._binding_post("IF_ACTION=Apply")


def test_reboot_signs_the_restart_body_and_accepts_succ(monkeypatch) -> None:
    session = _FakeRouterSession()
    client = _signed_client(monkeypatch, session)

    client.reboot()

    assert session.gets == ["http://h/", "http://h/?_type=menuView&_tag=rebootAndReset"]
    (post,) = session.posts
    assert post["url"] == "http://h" + client._REBOOT_FEED
    assert post["data"] == "IF_ACTION=Restart&_sessionTOKEN=ROT"
    assert post["headers"]["Referer"] == "http://h/?_type=menuView&_tag=rebootAndReset"
    assert post["headers"]["Check"] == _expected_check(post["data"])


def test_signed_post_falls_back_to_the_home_token(monkeypatch) -> None:
    session = _FakeRouterSession(page_html="page without a token")
    client = _signed_client(monkeypatch, session)
    client.reboot()
    assert session.posts[0]["data"] == "IF_ACTION=Restart&_sessionTOKEN=HOME"


def test_binding_post_without_a_key_raises_its_own_message(monkeypatch) -> None:
    client = _signed_client(monkeypatch, _FakeRouterSession(home_html="no key here"))
    with pytest.raises(
        network_router.NetworkCommandError,
        match="router binding write: missing session token or key",
    ):
        client._binding_post("IF_ACTION=Apply")


def test_reboot_treats_a_dropped_post_connection_as_accepted(monkeypatch) -> None:
    import requests

    session = _FakeRouterSession(post_exc=requests.ConnectionError("router went down"))
    client = _signed_client(monkeypatch, session)
    client.reboot()  # no raise: the device drops the connection as it restarts
    assert len(session.posts) == 1


def test_reboot_does_not_swallow_a_failure_before_the_post(monkeypatch) -> None:
    """Only the restart POST's own transport error means "accepted" — a router
    unreachable before anything was sent must still fail loudly."""
    import requests

    session = _FakeRouterSession(get_exc=requests.ConnectionError("unreachable"))
    client = _signed_client(monkeypatch, session)
    with pytest.raises(requests.RequestException):
        client.reboot()
    assert session.posts == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"home_html": "no key here"}, "router reboot: missing session token or key"),
        ({"post_text": "SessionTimeout"}, "session/token"),
        ({"post_text": "<IF_ERRORSTR>Busy</IF_ERRORSTR>"}, "rejected reboot: Busy"),
    ],
)
def test_reboot_rejections_raise_network_command_error(monkeypatch, kwargs, match) -> None:
    client = _signed_client(monkeypatch, _FakeRouterSession(**kwargs))
    with pytest.raises(network_router.NetworkCommandError, match=match):
        client.reboot()
