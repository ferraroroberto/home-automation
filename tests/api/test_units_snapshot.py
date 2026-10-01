"""``/api/units`` from the in-memory snapshot (#758): one MELCloud fetch
serves repeated reads, the answer carries its age, and a control write is
visible on the very next read. Cloud fetch and control are monkeypatched."""

from __future__ import annotations

from dataclasses import replace
from typing import List

import pytest
from fastapi.testclient import TestClient

from src.melcloud_client import DeviceInfo

_UNIT = DeviceInfo(
    unit_id="unit-x",
    name="Fixture Office",
    building="Fixture",
    power=True,
    operation_mode="Cool",
    room_temperature=22.0,
    set_temperature=24.0,
    fan_speed="Auto",
    operation_modes=["Heat", "Cool"],
    fan_speeds=["Auto", "One"],
    temp_ranges={"Cool": (16.0, 31.0)},
)


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    calls: List[int] = []

    async def fake_fetch_devices() -> List[DeviceInfo]:
        calls.append(1)
        return [_UNIT]

    monkeypatch.setattr("app.webapp.routers.units.fetch_devices", fake_fetch_devices)
    return calls


def test_repeat_reads_share_one_fetch_and_carry_their_age(
    client: TestClient, fetches: List[int]
) -> None:
    first = client.get("/api/units").json()
    second = client.get("/api/units").json()
    assert len(fetches) == 1
    assert second["units"] == first["units"]
    assert set(second["snapshot"]) == {"built_at", "age_seconds"}
    assert second["snapshot"]["age_seconds"] >= 0


def test_control_write_is_visible_on_the_next_read(
    client: TestClient, fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/api/units").json()["units"][0]["set_temperature"] == 24.0

    async def fake_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        return replace(_UNIT, set_temperature=kwargs["set_temperature"])

    monkeypatch.setattr("app.webapp.routers.units.set_device_state", fake_set_device_state)
    assert client.post("/api/units/unit-x", json={"set_temperature": 26.0}).status_code == 200

    unit = client.get("/api/units").json()["units"][0]
    assert unit["set_temperature"] == 26.0
    assert len(fetches) == 1  # the read-back, not a refetch


def test_failed_control_write_forces_a_refetch(
    client: TestClient, fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/api/units")

    async def failing_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        raise RuntimeError("cloud hiccup after the write landed")

    monkeypatch.setattr("app.webapp.routers.units.set_device_state", failing_set_device_state)
    assert client.post("/api/units/unit-x", json={"power": False}).status_code == 502

    client.get("/api/units")
    assert len(fetches) == 2


def test_automation_write_forces_a_refetch(
    client: TestClient, fetches: List[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.webapp import automation

    client.get("/api/units")

    async def fake_set_device_state(unit_id: str, **kwargs) -> DeviceInfo:
        return replace(_UNIT, **kwargs)

    monkeypatch.setattr(automation, "set_device_state", fake_set_device_state)
    asyncio.run(automation._write_unit("unit-x", set_temperature=25.0))

    client.get("/api/units")
    assert len(fetches) == 2
