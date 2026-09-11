"""API smoke for the DHCP planning + reservation endpoints (issue #176).

``GET/POST /api/network/dhcp-plan[...]`` and the reservation-batch routes
classify devices, compute planned IPs, and write only the pending rows.
Core fetch + router I/O are monkeypatched throughout — never a real LAN
scan or router login.
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


def test_dhcp_plan_route_classifies_and_assigns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/network/dhcp-plan`` groups devices by category with planned IPs.

    Core fetch + config are faked — no LAN I/O, no on-disk config. The route folds
    the display-name overrides + OUI vendor into the classifier (issue #170).
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="B8:27:EB:11:22:33", ip="192.168.0.50", name="nas",
                conn_type="wired", signal=None, link_rate=1000, ssid=None, source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:CC", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
            NetDevice(
                mac="00:11:22:33:44:55", ip="192.168.0.9", name="mystery",
                conn_type="wired", signal=None, link_rate=100, ssid=None, source="ap",
            ),
        ),
        alerts=(),
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return state

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Infra", 2, 10), CategoryRange("Cameras", 21, 30)),
        rules=(("Infra", ("router", "nas")), ("Cameras", ("camera",))),
        overrides={},
    )

    # The NAS already has a static reservation at its planned IP; the camera does
    # not — so the route should mark them "reserved" vs "create" (issue #176).
    async def fake_fetch_bindings() -> list:
        return [{"name": "nas", "mac": "B8:27:EB:11:22:33", "ip": "192.168.0.2", "inst_id": "X1"}]

    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.fetch_network_state", fake_fetch_network_state
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.load_network_display_names", lambda: {}
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.fetch_dhcp_bindings", fake_fetch_bindings
    )

    resp = client.get("/api/network/dhcp-plan")
    assert resp.status_code == 200
    body = resp.json()

    cats = {c["label"]: c for c in body["categories"]}
    infra = {a["mac"]: a for a in cats["Infra"]["assignments"]}
    cams = {a["mac"]: a for a in cats["Cameras"]["assignments"]}
    # The NAS is reserved at .2 → kept there and flagged already-reserved.
    assert infra["B8:27:EB:11:22:33"]["planned_ip"] == "192.168.0.2"
    assert infra["B8:27:EB:11:22:33"]["status"] == "reserved"
    # The camera has no IP / no binding → lowest free in its range, a new write.
    assert cams["5C:CF:7F:AA:BB:CC"]["planned_ip"] == "192.168.0.21"
    assert cams["5C:CF:7F:AA:BB:CC"]["status"] == "create"
    # Only the camera needs writing; the reserved NAS does not.
    assert body["pending_count"] == 1
    # The unclassified host lands in unassigned, never assigned an IP.
    assert [a["mac"] for a in body["unassigned"]] == ["00:11:22:33:44:55"]
    assert body["unassigned"][0]["planned_ip"] is None
    # The router's live bindings are surfaced for the "on the router now" list
    # (issue #176 step 1): the NAS reservation, flagged online + in the plan.
    assert body["bindings_known"] is True
    existing = {e["mac"]: e for e in body["existing"]}
    assert existing["B8:27:EB:11:22:33"]["online"] is True
    assert existing["B8:27:EB:11:22:33"]["in_plan"] is True
    assert existing["B8:27:EB:11:22:33"]["inst_id"] == "X1"
    # Category labels drive the "assign a group" dropdown on unassigned rows (step 3).
    assert body["category_labels"] == ["Infra", "Cameras"]


def test_dhcp_apply_route_writes_only_pending_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/network/dhcp-plan/apply`` writes create/change rows, not reserved.

    Recomputes the plan server-side and pushes only the rows that need it, one at a
    time, returning a per-row result. Router I/O is monkeypatched — nothing touches
    a real gateway (issue #176).
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    state = NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="B8:27:EB:11:22:33", ip="192.168.0.2", name="nas",
                conn_type="wired", signal=None, link_rate=1000, ssid=None, source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:CC", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
        ),
        alerts=(),
    )
    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Infra", 2, 10), CategoryRange("Cameras", 21, 30)),
        rules=(("Infra", ("router", "nas")), ("Cameras", ("camera",))),
        overrides={},
    )

    async def fake_fetch_network_state(include_speedtest: bool = False) -> NetworkState:
        return state

    async def fake_fetch_bindings() -> list:
        # NAS already reserved at its planned .2 → reserved (skip); camera → create.
        return [{"name": "nas", "mac": "B8:27:EB:11:22:33", "ip": "192.168.0.2", "inst_id": "X1"}]

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.fetch_network_state", fake_fetch_network_state
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.load_network_display_names", lambda: {}
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.fetch_dhcp_bindings", fake_fetch_bindings
    )
    monkeypatch.setattr(
        "app.webapp.routers.dhcp_plan.apply_dhcp_bindings", fake_apply
    )

    resp = client.post("/api/network/dhcp-plan/apply")
    assert resp.status_code == 200
    body = resp.json()

    # Only the camera (create) is written — the reserved NAS is left untouched.
    assert [r["mac"] for r in written] == ["5C:CF:7F:AA:BB:CC"]
    assert written[0]["ip"] == "192.168.0.21"
    assert written[0]["name"]  # a non-empty, router-safe binding name
    assert body["applied"] == 1
    assert body["failed"] == 0
    assert body["results"][0]["ok"] is True


def _two_camera_state() -> "NetworkState":
    """Two unreserved cameras → two ``create`` rows (issue #176 step 2 fixture)."""
    return NetworkState(
        internet=InternetHealth(online=True),
        access_point=AccessPointHealth(reachable=True, device_count=2),
        router=RouterHealth(reachable=True, authenticated=True),
        wifi=WifiDiagnostics(available=False, error="no scan"),
        devices=(
            NetDevice(
                mac="5C:CF:7F:AA:BB:01", ip=None, name="front-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
            NetDevice(
                mac="5C:CF:7F:AA:BB:02", ip=None, name="back-camera",
                conn_type="5GHz", signal=60, link_rate=300, ssid="Home", source="ap",
            ),
        ),
        alerts=(),
    )


def test_dhcp_apply_route_selective_writes_only_listed_macs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../apply`` with a ``{"macs": [...]}`` body writes only those rows.

    Selective apply (issue #176 step 2): the user ticks which create/change rows to
    push when the 10-slot table can't hold them all. The plan is recomputed
    server-side; only the listed MAC is written. Router I/O is monkeypatched.
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(("Cameras", ("camera",)),),
        overrides={},
    )

    async def fake_state(include_speedtest: bool = False) -> NetworkState:
        return _two_camera_state()

    async def fake_bindings() -> list:
        return []

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr("app.webapp.routers.dhcp_plan.fetch_network_state", fake_state)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_network_display_names", lambda: {})
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.apply_dhcp_bindings", fake_apply)

    # Lower-case input proves the MAC allow-list is normalised before matching.
    resp = client.post(
        "/api/network/dhcp-plan/apply", json={"macs": ["5c:cf:7f:aa:bb:01"]}
    )
    assert resp.status_code == 200
    assert [r["mac"] for r in written] == ["5C:CF:7F:AA:BB:01"]
    assert resp.json()["applied"] == 1


def test_dhcp_delete_route_validates_and_calls_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-bindings/delete`` frees a slot by ``inst_id`` (issue #176 step 1)."""
    captured: dict = {}

    async def fake_delete(inst_id: str) -> bool:
        captured["inst_id"] = inst_id
        return True

    monkeypatch.setattr("app.webapp.routers.dhcp_plan.delete_dhcp_binding", fake_delete)

    resp = client.post(
        "/api/network/dhcp-bindings/delete", json={"inst_id": "DEV.V4DP.Sr.Pl1.Bd3"}
    )
    assert resp.status_code == 200
    assert captured["inst_id"] == "DEV.V4DP.Sr.Pl1.Bd3"
    # A malformed id is rejected (400) before any router call.
    resp_bad = client.post(
        "/api/network/dhcp-bindings/delete", json={"inst_id": "bad id; rm -rf"}
    )
    assert resp_bad.status_code == 400


def test_dhcp_override_route_persists_and_validates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PUT .../dhcp-overrides/{mac}`` saves a per-MAC group choice (issue #176 step 3)."""
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(),
        overrides={},
    )
    saved: dict = {}

    def fake_set(mac: str, category: str) -> None:
        saved[mac] = category

    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.set_dhcp_override", fake_set)

    resp = client.put(
        "/api/network/dhcp-overrides/5C:CF:7F:AA:BB:01", json={"category": "Cameras"}
    )
    assert resp.status_code == 200
    assert saved == {"5C:CF:7F:AA:BB:01": "Cameras"}
    # An unknown category is rejected (400) — must be one of the configured ranges.
    resp_bad = client.put(
        "/api/network/dhcp-overrides/5C:CF:7F:AA:BB:01", json={"category": "Nope"}
    )
    assert resp_bad.status_code == 400


def test_dhcp_manual_add_validates_and_writes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-bindings`` adds one validated reservation (issue #176 step 3)."""
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(),
        overrides={},
    )

    async def fake_bindings() -> list:
        return [{"name": "x", "mac": "00:00:00:00:00:09", "ip": "192.168.0.9", "inst_id": "B1"}]

    written: list = []

    async def fake_apply(rows: list) -> list:
        written.extend(rows)
        return [{"mac": r["mac"], "ip": r["ip"], "ok": True, "error": None} for r in rows]

    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.apply_dhcp_bindings", fake_apply)

    # A wrong-subnet IP is rejected (400) before any router call.
    bad = client.post(
        "/api/network/dhcp-bindings", json={"mac": "AA:BB:CC:DD:EE:01", "ip": "10.0.0.5"}
    )
    assert bad.status_code == 400
    # An IP already held by a *different* MAC is a conflict (409).
    dup = client.post(
        "/api/network/dhcp-bindings", json={"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.9"}
    )
    assert dup.status_code == 409
    # A valid add writes exactly one binding row.
    good = client.post(
        "/api/network/dhcp-bindings",
        json={"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.25", "name": "Cam"},
    )
    assert good.status_code == 200
    assert [r["mac"] for r in written] == ["AA:BB:CC:DD:EE:01"]
    assert written[0]["ip"] == "192.168.0.25"


def test_dhcp_reservations_apply_removes_then_adds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../dhcp-reservations/apply`` applies a staged batch (issue #176 redesign).

    The user marks router rows to remove, plan rows + manual rows to add; the route
    recomputes the plan MACs' IPs server-side and hands one combined batch to
    ``apply_dhcp_changes`` (deletes first, then adds). Router I/O is monkeypatched.
    """
    from src.dhcp_plan import CategoryRange, DhcpPlanConfig

    config = DhcpPlanConfig(
        subnet_prefix="192.168.0",
        ranges=(CategoryRange("Cameras", 21, 30),),
        rules=(("Cameras", ("camera",)),),
        overrides={},
    )

    async def fake_state(include_speedtest: bool = False) -> NetworkState:
        return _two_camera_state()

    async def fake_bindings() -> list:
        return [{"name": "old", "mac": "00:00:00:00:00:09", "ip": "192.168.0.9", "inst_id": "B9"}]

    captured: dict = {}

    async def fake_changes(removes: list, adds: list) -> list:
        captured["removes"] = list(removes)
        captured["adds"] = list(adds)
        out = [{"op": "remove", "inst_id": r, "ok": True, "error": None} for r in removes]
        out += [{"op": "add", "mac": a["mac"], "ip": a["ip"], "ok": True, "error": None} for a in adds]
        return out

    monkeypatch.setattr("app.webapp.routers.dhcp_plan.fetch_network_state", fake_state)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_network_display_names", lambda: {})
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.load_dhcp_plan_config", lambda: config)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.fetch_dhcp_bindings", fake_bindings)
    monkeypatch.setattr("app.webapp.routers.dhcp_plan.apply_dhcp_changes", fake_changes)

    resp = client.post(
        "/api/network/dhcp-reservations/apply",
        json={
            "remove": ["B9"],
            "add_macs": ["5c:cf:7f:aa:bb:01"],
            "add_manual": [{"mac": "AA:BB:CC:DD:EE:01", "ip": "192.168.0.40", "name": "Manual"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # The remove is passed through; both a plan MAC and a manual row are added.
    assert captured["removes"] == ["B9"]
    add_macs = [a["mac"] for a in captured["adds"]]
    assert "5C:CF:7F:AA:BB:01" in add_macs and "AA:BB:CC:DD:EE:01" in add_macs
    # The plan row's IP is recomputed server-side (lowest free in the camera range).
    plan_row = next(a for a in captured["adds"] if a["mac"] == "5C:CF:7F:AA:BB:01")
    assert plan_row["ip"] == "192.168.0.21"
    assert body["removed"] == 1 and body["added"] == 2 and body["failed"] == 0
    # A bad manual IP is rejected (400) before any router call.
    bad = client.post(
        "/api/network/dhcp-reservations/apply",
        json={"add_manual": [{"mac": "AA:BB:CC:DD:EE:02", "ip": "10.0.0.1"}]},
    )
    assert bad.status_code == 400
