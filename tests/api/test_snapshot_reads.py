"""Security, Hyper-V, energy and Tuya reads from their in-memory snapshots
(#759): repeated reads share one fetch and carry their age, and every write
path, in a router or in an automation, shows on the very next read.

Every upstream (RISCO, Hyper-V, FusionSolar/Modbus, the Tuya LAN) is faked.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.hyperv_client import HyperVState
from src.huawei_client import EnergyState
from src.risco_client import SecurityState, SecurityZone
from src.tuya_client import TuyaDeviceInfo

_DISARMED = SecurityState(
    reachable=True,
    label="Disarmed",
    mode="disarmed",
    zones=[SecurityZone(id=4, name="Garage", type=2)],
)


@pytest.fixture
def security_fetches(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    calls: List[int] = []

    async def fake_fetch() -> SecurityState:
        calls.append(1)
        return _DISARMED

    monkeypatch.setattr("app.webapp.routers.security.fetch_security_state", fake_fetch)
    return calls


def test_security_reads_share_one_fetch_and_carry_their_age(
    client: TestClient, security_fetches: List[int]
) -> None:
    client.get("/api/security")
    body = client.get("/api/security").json()
    assert len(security_fetches) == 1
    assert body["mode"] == "disarmed"
    assert set(body["snapshot"]) == {"built_at", "age_seconds"}


def test_security_action_is_visible_on_the_next_read(
    client: TestClient, security_fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/api/security")

    async def fake_control(action: str) -> SecurityState:
        return replace(_DISARMED, mode="armed", label="Armed")

    async def no_record(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.webapp.routers.security.control_system", fake_control)
    monkeypatch.setattr("app.webapp.routers.security.note_manual_alarm_action", lambda _a: None)
    monkeypatch.setattr("app.webapp.routers.security.record_alarm_action", no_record)
    assert client.post("/api/security/arm").status_code == 200

    assert client.get("/api/security").json()["mode"] == "armed"
    assert len(security_fetches) == 1  # the read-back, not a refetch


def test_zone_bypass_is_visible_on_the_next_read(
    client: TestClient, security_fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/api/security")

    async def fake_bypass(zone_id: int, bypass: bool) -> SecurityState:
        return replace(_DISARMED, zones=[replace(_DISARMED.zones[0], bypassed=bypass)])

    monkeypatch.setattr("app.webapp.routers.security.set_zone_bypass", fake_bypass)
    assert client.post("/api/security/zones/4/bypass", json={"bypass": True}).status_code == 200

    assert client.get("/api/security").json()["zones"][0]["bypassed"] is True


def test_automation_alarm_and_bypass_writes_force_a_refetch(
    client: TestClient, security_fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.webapp import alarm_notify, security_override_automation

    async def fake_control(action: str) -> SecurityState:
        return _DISARMED

    async def fake_bypass(zone_id: int, bypass: bool) -> SecurityState:
        return _DISARMED

    monkeypatch.setattr(alarm_notify, "control_system", fake_control)
    monkeypatch.setattr(security_override_automation, "set_zone_bypass", fake_bypass)

    client.get("/api/security")
    asyncio.run(alarm_notify._send("arm"))
    client.get("/api/security")
    assert len(security_fetches) == 2

    asyncio.run(security_override_automation._set_bypass(4, True))
    client.get("/api/security")
    assert len(security_fetches) == 3


def test_hyperv_start_is_visible_on_the_next_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = HyperVState(available=True, name="Home Assistant", state="off")
    fetches: List[int] = []

    def fake_fetch() -> HyperVState:
        fetches.append(1)
        return off

    monkeypatch.setattr("app.webapp.routers.hyperv.fetch_hyperv_state", fake_fetch)
    from app.webapp.routers import hyperv

    monkeypatch.setitem(hyperv._ACTIONS, "start", lambda: replace(off, state="running"))

    assert client.get("/api/hyperv").json()["snapshot"]["age_seconds"] >= 0
    assert client.post("/api/hyperv/start").status_code == 200
    assert client.get("/api/hyperv").json()["hyperv"]["state"] == "running"
    assert len(fetches) == 1


def test_energy_reads_share_one_fetch_and_carry_their_age(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches: List[int] = []

    async def fake_fetch() -> EnergyState:
        fetches.append(1)
        return EnergyState()

    monkeypatch.setattr("app.webapp.routers.energy.fetch_energy_state", fake_fetch)
    client.get("/api/energy")
    body = client.get("/api/energy").json()
    assert len(fetches) == 1
    assert set(body["snapshot"]) == {"built_at", "age_seconds"}


def test_tuya_switch_is_visible_on_the_next_read_and_names_stay_live(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.webapp import actions_registry

    info = TuyaDeviceInfo(
        device_id="plug-1", name="Fixture Plug", ip="10.0.0.9",
        has_valid_ip=True, has_local_key=True, switch_dps="1",
    )
    plug: Dict[str, Any] = {"on": False, "reads": 0}

    def fake_read(device_id: str) -> Dict[str, Any]:
        plug["reads"] += 1
        return {"switch_on": plug["on"]}

    def fake_switch(device_id: str, on: bool) -> None:
        plug["on"] = on

    names: Dict[str, str] = {}
    monkeypatch.setattr("app.webapp.routers.tuya.list_devices", lambda: [info])
    monkeypatch.setattr("app.webapp.routers.tuya.read_device_state", fake_read)
    monkeypatch.setattr("app.webapp.routers.tuya.set_switch", fake_switch)
    monkeypatch.setattr("app.webapp.routers.tuya.load_tuya_display_names", lambda: dict(names))
    monkeypatch.setattr("app.webapp.routers.tuya.load_hidden_tuya_ids", lambda: set())

    assert client.get("/api/tuya").json()["devices"][0]["switch_on"] is False
    assert client.post("/api/tuya/plug-1/switch", json={"on": True}).status_code == 200
    reads_after_write = plug["reads"]  # the handler's own read-back
    names["plug-1"] = "Kettle"  # a rename is a per-request decoration
    card = client.get("/api/tuya").json()["devices"][0]
    assert card["switch_on"] is True
    assert card["display_name"] == "Kettle"
    assert plug["reads"] == reads_after_write  # served from the snapshot

    # A Stream Deck plug action outside the router forces a refetch.
    monkeypatch.setattr(actions_registry, "set_switch", fake_switch)
    asyncio.run(actions_registry._plug_action("plug-1", False))
    assert client.get("/api/tuya").json()["devices"][0]["switch_on"] is False


def test_ups_reads_share_one_fetch_and_a_failed_read_shows_as_error_not_latency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#769: ``upsc`` ran inside every request and its 5 s timeout hit one a minute."""
    from src.ups_client import UpsState

    fetches: List[int] = []
    failed = UpsState(available=False, source="none", error="upsc timed out")

    def fake_fetch() -> UpsState:
        fetches.append(1)
        return failed

    monkeypatch.setattr("app.webapp.routers.ups.fetch_ups_state", fake_fetch)
    client.get("/api/ups")
    body = client.get("/api/ups").json()
    assert len(fetches) == 1
    assert body["ups"]["error"] == "upsc timed out"
    assert set(body["snapshot"]) == {"built_at", "age_seconds"}


def test_a_slow_ups_tick_never_blocks_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background refresh can sit in a 5 s ``upsc`` timeout; a read still answers at once."""
    import threading

    from app.webapp.routers import ups
    from src.ups_client import UpsState

    release = threading.Event()
    slow = {"on": False}

    def fake_fetch() -> UpsState:
        if slow["on"]:
            release.wait(10)
        return UpsState(available=True, source="nut", status="online")

    monkeypatch.setattr("app.webapp.routers.ups.fetch_ups_state", fake_fetch)

    async def run() -> None:
        await ups.UPS_SNAPSHOT.read()  # seeds the value and marks demand
        slow["on"] = True
        tick = asyncio.create_task(ups.UPS_SNAPSHOT.tick_once())
        await asyncio.sleep(0.05)  # the tick is now parked inside the slow read
        state, meta = await asyncio.wait_for(ups.UPS_SNAPSHOT.read(), timeout=1.0)
        assert state.status == "online" and meta["age_seconds"] < ups.UPS_SNAPSHOT.max_age_s
        release.set()
        await tick

    asyncio.run(run())
