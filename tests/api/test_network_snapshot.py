"""``GET /api/network`` and ``GET /api/circuits`` answer from memory (#926).

The NETGEAR AP's SOAP read takes ~8 s and every request used to sit in it, so
the Devices tab was still loading 10 s after the page opened. Both reads now
come from a serve-stale :class:`~app.webapp.read_snapshot.ReadSnapshot`: once
a value exists a read answers at once and the one shared refresh runs behind
it. Neither snapshot ticks — a refresh only ever follows a read, so the AP,
the router and the meters see no more reads than before.

Every upstream (AP, router, host probes, Athom meters) is faked.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.athom_client import CircuitReading, CircuitsState, MeterState
from src.network_client import (
    AccessPointHealth,
    InternetHealth,
    NetDevice,
    NetworkState,
    RouterHealth,
    WifiDiagnostics,
)


def _age(snap: Any, seconds: float) -> None:
    """Make the held value (and the last refresh attempt) *seconds* older.

    Not by patching ``time.monotonic``: that is the event loop's clock too, and
    every ``asyncio.sleep`` would stall.
    """
    snap._built -= seconds
    snap._attempted -= seconds


def _network_state(name: str) -> NetworkState:
    return NetworkState(
        internet=InternetHealth(online=True, external_ms=12.0),
        access_point=AccessPointHealth(reachable=True, device_count=1),
        router=RouterHealth(reachable=True, authenticated=True),
        devices=(
            NetDevice(
                mac="00:11:22:00:00:01",
                ip="192.0.2.11",
                name=name,
                conn_type="5GHz",
                signal=70,
                link_rate=300,
                ssid="Home",
                source="ap",
            ),
        ),
        wifi=WifiDiagnostics(available=False),
        alerts=(),
    )


@pytest.fixture
def slow_network(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A fake AP/router read that can be parked mid-read, like the real 8 s one."""
    ctl: Dict[str, Any] = {"calls": 0, "slow": False, "release": None, "speedtest": []}

    async def fake_fetch(include_speedtest: bool = False) -> NetworkState:
        ctl["calls"] += 1
        ctl["speedtest"].append(include_speedtest)
        if ctl["slow"]:
            await asyncio.wait_for(ctl["release"].wait(), 10)
        return _network_state(f"read {ctl['calls']}")

    monkeypatch.setattr("app.webapp.routers.network.fetch_network_state", fake_fetch)
    return ctl


def test_a_warm_network_read_answers_at_once_while_the_ap_read_runs(
    slow_network: Dict[str, Any],
) -> None:
    from app.webapp.routers import network

    async def run() -> None:
        first = await network.get_network(speedtest=False)
        assert first["devices"][0]["name"] == "read 1"

        slow_network["slow"] = True
        slow_network["release"] = asyncio.Event()
        _age(network.NETWORK_SNAPSHOT, 120)  # well past any freshness bound
        body = await asyncio.wait_for(network.get_network(speedtest=False), timeout=1.0)
        assert body["devices"][0]["name"] == "read 1"  # the last reading, at once
        assert body["snapshot"]["stale"] is True  # and honestly marked old

        slow_network["release"].set()
        await asyncio.sleep(0.05)  # the background refresh lands
        body = await network.get_network(speedtest=False)
        assert body["devices"][0]["name"] == "read 2"

    asyncio.run(run())
    assert slow_network["calls"] == 2


def test_concurrent_network_readers_share_one_ap_read(slow_network: Dict[str, Any]) -> None:
    """Six screens opening the Devices tab at once cost the AP one read, not six."""
    from app.webapp.routers import network

    async def run() -> None:
        bodies = await asyncio.gather(*(network.get_network(speedtest=False) for _ in range(6)))
        assert {b["devices"][0]["name"] for b in bodies} == {"read 1"}

    asyncio.run(run())
    assert slow_network["calls"] == 1


def test_network_snapshot_never_polls_the_ap_on_its_own(
    slow_network: Dict[str, Any],
) -> None:
    """No tick: with nobody reading, the AP and router are left alone."""
    from app.webapp.routers import network

    async def run() -> None:
        await network.get_network(speedtest=False)
        for _ in range(5):
            _age(network.NETWORK_SNAPSHOT, network.NETWORK_SNAPSHOT.tick_s)
            await network.NETWORK_SNAPSHOT.tick_once()

    asyncio.run(run())
    assert slow_network["calls"] == 1


def test_device_history_is_recorded_once_per_real_read(
    client: TestClient, slow_network: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached answer is not a sighting: ``times_seen`` counts AP reads only."""
    recorded: List[int] = []
    import src.network_history as nh

    real = nh.record_and_snapshot

    def counting(seen, now):  # noqa: ANN001 — mirrors the patched signature
        recorded.append(1)
        return real(seen, now)

    monkeypatch.setattr("app.webapp.routers.network.record_and_snapshot", counting)
    for _ in range(3):
        body = client.get("/api/network").json()
    assert slow_network["calls"] == 1
    assert len(recorded) == 1
    assert body["devices"][0]["times_seen"] == 1


def test_important_flag_shows_on_the_next_cached_read(
    client: TestClient, slow_network: Dict[str, Any]
) -> None:
    client.get("/api/network")
    resp = client.post(
        "/api/network/devices/00:11:22:00:00:01/important", json={"important": True}
    )
    assert resp.status_code == 200
    body = client.get("/api/network").json()
    assert slow_network["calls"] == 1
    assert body["devices"][0]["important"] is True


def test_speedtest_is_a_live_read_never_served_from_memory(
    client: TestClient, slow_network: Dict[str, Any]
) -> None:
    client.get("/api/network")
    body = client.get("/api/network?speedtest=1").json()
    assert slow_network["speedtest"] == [False, True]
    assert "snapshot" not in body  # a live measurement, not an answer from memory


# ------------------------------------------------------------------ circuits
def _circuits(power: float) -> CircuitsState:
    return CircuitsState(
        meters=[
            MeterState(
                meter_id="AA:BB:CC:00:00:01",
                name="Panel",
                reachable=True,
                channels=[CircuitReading(channel=1, key="AA:BB:CC:00:00:01:1", power_w=power)],
            )
        ],
        discovery_ok=True,
    )


@pytest.fixture
def slow_circuits(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    ctl: Dict[str, Any] = {"calls": 0, "slow": False, "release": None}

    async def fake_fetch(force: bool = False) -> CircuitsState:
        ctl["calls"] += 1
        if ctl["slow"]:
            await asyncio.wait_for(ctl["release"].wait(), 10)
        return _circuits(float(ctl["calls"]))

    monkeypatch.setattr("app.webapp.routers.circuits.fetch_circuits_state", fake_fetch)
    return ctl


def test_a_warm_circuits_read_answers_at_once_while_the_meters_are_read(
    slow_circuits: Dict[str, Any],
) -> None:
    from app.webapp.routers import circuits

    async def run() -> None:
        await circuits.get_circuits()
        slow_circuits["slow"] = True
        slow_circuits["release"] = asyncio.Event()
        _age(circuits.CIRCUITS_SNAPSHOT, 120)
        body = await asyncio.wait_for(circuits.get_circuits(), timeout=1.0)
        assert body["meters"][0]["channels"][0]["power_w"] == 1.0
        assert body["snapshot"]["stale"] is True
        slow_circuits["release"].set()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert slow_circuits["calls"] == 2


def test_circuit_sign_flip_refetches_on_the_next_read(
    client: TestClient, slow_circuits: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flipped clamp changes how readings are interpreted, so it can't be served from memory."""
    monkeypatch.setattr("app.webapp.routers.circuits.set_circuit_inverted", lambda _k, _v: None)
    client.get("/api/circuits")
    resp = client.put("/api/circuits/AA:BB:CC:00:00:01:1/invert", json={"invert": True})
    assert resp.status_code == 200
    client.get("/api/circuits")
    assert slow_circuits["calls"] == 2
