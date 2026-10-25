"""``GET /api/ha`` and ``GET /api/lights`` answer from memory (#928).

The same treatment #926 gave ``/api/network``: Home Assistant's satellite read
(a cold websocket round trip, ~8 s) and the Elgato read (a 3 s mDNS discovery
sweep on every call) used to sit in every Devices-tab load. Both now come from
a serve-stale :class:`~app.webapp.read_snapshot.ReadSnapshot`: once a value
exists a read answers at once and the one shared refresh runs behind it.
Neither snapshot ticks — a refresh only ever follows a read, so Home Assistant
and the lights see no more reads than before.

Every upstream (Home Assistant, the lights) is faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.devices.elgato_client import ElgatoLight


def _age(snap: Any, seconds: float) -> None:
    """Make the held value (and the last refresh attempt) *seconds* older.

    Not by patching ``time.monotonic``: that is the event loop's clock too.
    """
    snap._built -= seconds
    snap._attempted -= seconds


# ------------------------------------------------------------------- lights
def _light(brightness: int, *, light_id: str = "192.0.2.10:9123") -> ElgatoLight:
    return ElgatoLight(
        light_id=light_id,
        host=light_id.split(":")[0],
        port=9123,
        name="Fixture Key Light",
        product_name="Elgato Key Light",
        firmware="1.0",
        on=True,
        brightness=brightness,
        temperature=200,
        temperature_k=5000,
        supports_temperature=True,
        mac_address="AA:BB:CC:DD:EE:FF",
    )


@pytest.fixture
def slow_lights(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A fake Elgato read that can be parked mid-read, like the real 3 s sweep."""
    ctl: Dict[str, Any] = {"calls": 0, "slow": False, "release": None}

    async def fake_fetch_lights() -> List[ElgatoLight]:
        ctl["calls"] += 1
        if ctl["slow"]:
            await asyncio.wait_for(ctl["release"].wait(), 10)
        return [_light(ctl["calls"])]

    monkeypatch.setattr("app.webapp.routers.lights.fetch_lights", fake_fetch_lights)
    monkeypatch.setattr("app.webapp.routers.lights.load_elgato_display_names", lambda: {})
    return ctl


def test_a_warm_lights_read_answers_at_once_while_the_sweep_runs(
    slow_lights: Dict[str, Any],
) -> None:
    from app.webapp.routers import lights

    async def run() -> None:
        first = await lights.list_lights()
        assert first["lights"][0]["brightness"] == 1

        slow_lights["slow"] = True
        slow_lights["release"] = asyncio.Event()
        _age(lights.LIGHTS_SNAPSHOT, 120)  # well past any freshness bound
        body = await asyncio.wait_for(lights.list_lights(), timeout=1.0)
        assert body["lights"][0]["brightness"] == 1  # the last reading, at once
        assert body["snapshot"]["stale"] is True  # and honestly marked old

        slow_lights["release"].set()
        await asyncio.sleep(0.05)  # the background refresh lands
        body = await lights.list_lights()
        assert body["lights"][0]["brightness"] == 2

    asyncio.run(run())
    assert slow_lights["calls"] == 2


def test_concurrent_lights_readers_share_one_sweep(slow_lights: Dict[str, Any]) -> None:
    """Six screens opening the Devices tab at once cost one discovery, not six."""
    from app.webapp.routers import lights

    async def run() -> None:
        bodies = await asyncio.gather(*(lights.list_lights() for _ in range(6)))
        assert {b["lights"][0]["brightness"] for b in bodies} == {1}

    asyncio.run(run())
    assert slow_lights["calls"] == 1


def test_lights_snapshot_never_sweeps_on_its_own(slow_lights: Dict[str, Any]) -> None:
    """No tick: with nobody reading, the lights are left alone."""
    from app.webapp.routers import lights

    async def run() -> None:
        await lights.list_lights()
        for _ in range(5):
            _age(lights.LIGHTS_SNAPSHOT, lights.LIGHTS_SNAPSHOT.tick_s)
            await lights.LIGHTS_SNAPSHOT.tick_once()

    asyncio.run(run())
    assert slow_lights["calls"] == 1


def test_repeat_lights_reads_inside_the_window_cost_one_sweep(
    client: TestClient, slow_lights: Dict[str, Any]
) -> None:
    for _ in range(3):
        assert client.get("/api/lights").status_code == 200
    assert slow_lights["calls"] == 1


def test_a_light_write_shows_on_the_next_cached_read(
    client: TestClient, slow_lights: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write's read-back patches the held list: no sweep, no stale brightness."""

    async def fake_set(light_id: str, **kwargs: Any) -> ElgatoLight:
        return dataclasses.replace(_light(77, light_id=light_id), on=bool(kwargs.get("on")))

    monkeypatch.setattr("app.webapp.routers.lights.set_light_state", fake_set)
    assert client.get("/api/lights").json()["lights"][0]["brightness"] == 1
    resp = client.post("/api/lights/192.0.2.10:9123", json={"on": False, "brightness": 77})
    assert resp.status_code == 200
    body = client.get("/api/lights").json()
    assert body["lights"][0]["brightness"] == 77
    assert body["lights"][0]["on"] is False
    assert slow_lights["calls"] == 1


def test_display_name_shows_on_the_next_cached_read(
    client: TestClient, slow_lights: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names are read per request, so a rename needs no sweep."""
    names: Dict[str, str] = {}
    monkeypatch.setattr("app.webapp.routers.lights.load_elgato_display_names", lambda: names)
    client.get("/api/lights")
    names["mac:AA:BB:CC:DD:EE:FF"] = "Desk left"
    body = client.get("/api/lights").json()
    assert body["lights"][0]["display_name"] == "Desk left"
    assert slow_lights["calls"] == 1


def test_lights_refresh_is_a_live_read(
    client: TestClient, slow_lights: Dict[str, Any]
) -> None:
    """The Refresh button re-runs discovery now, never an answer from memory."""
    client.get("/api/lights")
    body = client.post("/api/lights/refresh").json()
    assert slow_lights["calls"] == 2
    assert body["lights"][0]["brightness"] == 2
    assert body["refresh"]["safe"] is True


# ----------------------------------------------------------------------- ha
def _satellite(name: str) -> Dict[str, Any]:
    return {
        "entity_id": "assist_satellite.kitchen",
        "name": name,
        "room": "Kitchen",
        "online": True,
        "state": "idle",
        "volume": 0.5,
        "media_player": "media_player.kitchen",
    }


@pytest.fixture
def slow_ha(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A fake Home Assistant that can be parked mid-read, like the real 8 s one."""
    import app.webapp.routers.ha as router

    ctl: Dict[str, Any] = {"calls": 0, "slow": False, "release": None, "events": 0}

    class FakeHaClient:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def satellites(self) -> List[Dict[str, Any]]:
            ctl["calls"] += 1
            if ctl["slow"]:
                await asyncio.wait_for(ctl["release"].wait(), 10)
            return [_satellite(f"read {ctl['calls']}")]

    def fake_events(**_kwargs: Any) -> List[Dict[str, Any]]:
        ctl["events"] += 1
        return []

    monkeypatch.setattr(router, "HomeAssistantClient", FakeHaClient)
    monkeypatch.setattr(router.telemetry, "read_events", fake_events)
    return ctl


class _Req:
    """The slice of a Request ``get_home_assistant`` touches (no pooled session)."""

    class app:  # noqa: N801 — mimics ``request.app``
        class state:  # noqa: N801
            pass


def test_a_warm_ha_read_answers_at_once_while_home_assistant_is_read(
    slow_ha: Dict[str, Any],
) -> None:
    from app.webapp.routers import ha

    async def run() -> None:
        first = await ha.get_home_assistant(_Req())  # type: ignore[arg-type]
        assert first["satellites"][0]["name"] == "read 1"

        slow_ha["slow"] = True
        slow_ha["release"] = asyncio.Event()
        _age(ha.HA_SNAPSHOT, 120)
        body = await asyncio.wait_for(ha.get_home_assistant(_Req()), timeout=1.0)  # type: ignore[arg-type]
        assert body["satellites"][0]["name"] == "read 1"
        assert body["snapshot"]["stale"] is True

        slow_ha["release"].set()
        await asyncio.sleep(0.05)
        body = await ha.get_home_assistant(_Req())  # type: ignore[arg-type]
        assert body["satellites"][0]["name"] == "read 2"

    asyncio.run(run())
    assert slow_ha["calls"] == 2


def test_concurrent_ha_readers_share_one_home_assistant_read(slow_ha: Dict[str, Any]) -> None:
    from app.webapp.routers import ha

    async def run() -> None:
        bodies = await asyncio.gather(*(ha.get_home_assistant(_Req()) for _ in range(6)))  # type: ignore[arg-type]
        assert {b["satellites"][0]["name"] for b in bodies} == {"read 1"}

    asyncio.run(run())
    assert slow_ha["calls"] == 1


def test_ha_snapshot_never_polls_home_assistant_on_its_own(slow_ha: Dict[str, Any]) -> None:
    from app.webapp.routers import ha

    async def run() -> None:
        await ha.get_home_assistant(_Req())  # type: ignore[arg-type]
        for _ in range(5):
            _age(ha.HA_SNAPSHOT, ha.HA_SNAPSHOT.tick_s)
            await ha.HA_SNAPSHOT.tick_once()

    asyncio.run(run())
    assert slow_ha["calls"] == 1


def test_repeat_ha_reads_inside_the_window_cost_one_read(
    client: TestClient, slow_ha: Dict[str, Any]
) -> None:
    for _ in range(3):
        assert client.get("/api/ha").status_code == 200
    assert slow_ha["calls"] == 1


def test_ha_interactions_are_read_per_request_not_frozen(
    client: TestClient, slow_ha: Dict[str, Any]
) -> None:
    """The voice log is a local file: a new interaction must show on the next poll."""
    client.get("/api/ha")
    client.get("/api/ha")
    assert slow_ha["calls"] == 1
    assert slow_ha["events"] == 2
