"""Unit connectivity parsing (`isConnected`) — pure logic, no cloud.

``aiomelcloudhome``'s ``ATAUnit`` model rebuilds its dict from the raw API
payload and drops ``isConnected`` on the way, so ``melcloud_client`` reads the
flag off the raw ``/context`` JSON itself.  These cover the shapes that JSON
can take (issue #520), including the ones that must fail *open* — an unknown
flag has to leave the unit controllable rather than silently disable it.
"""

from __future__ import annotations

import logging

import pytest

from src import melcloud_client
from src.melcloud_client import DeviceInfo, _connectivity_map, _log_reachability_changes


def _payload(units, key: str = "buildings"):
    return {key: [{"id": "b1", "name": "Casa", "airToAirUnits": units}]}


def test_reads_is_connected_per_unit() -> None:
    raw = _payload(
        [
            {"id": "u1", "isConnected": True},
            {"id": "u2", "isConnected": False},
        ]
    )
    assert _connectivity_map(raw) == {"u1": True, "u2": False}


def test_guest_buildings_are_included() -> None:
    raw = _payload([{"id": "g1", "isConnected": False}], key="guestBuildings")
    assert _connectivity_map(raw) == {"g1": False}


def test_unit_without_the_flag_is_omitted() -> None:
    """Omitted, not False — the caller defaults a missing id to reachable."""
    raw = _payload([{"id": "u1", "rssi": -55}])
    assert _connectivity_map(raw) == {}


def test_ids_are_stringified() -> None:
    raw = _payload([{"id": 7, "isConnected": False}])
    assert _connectivity_map(raw) == {"7": False}


def _device(reachable: bool) -> DeviceInfo:
    return DeviceInfo(
        unit_id="u1", name="Fixture", building="Test", power=True,
        operation_mode="Cool", room_temperature=22.0, set_temperature=24.0,
        fan_speed="Auto", reachable=reachable,
    )


@pytest.fixture(autouse=True)
def _clear_reachability_state():
    melcloud_client._last_reachable.clear()
    yield
    melcloud_client._last_reachable.clear()


def test_offline_logs_once_not_once_per_poll(caplog: pytest.LogCaptureFixture) -> None:
    """The webapp polls every 30s; a unit down for days must not spam the log."""
    caplog.set_level(logging.INFO, logger="melcloud")
    for _ in range(5):
        _log_reachability_changes([_device(False)])
    assert sum("is offline" in r.message for r in caplog.records) == 1


def test_recovery_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    _log_reachability_changes([_device(False)])
    caplog.set_level(logging.INFO, logger="melcloud")
    for _ in range(3):
        _log_reachability_changes([_device(True)])
    assert sum("reachable again" in r.message for r in caplog.records) == 1


def test_healthy_unit_never_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="melcloud")
    for _ in range(3):
        _log_reachability_changes([_device(True)])
    assert [r for r in caplog.records if "reachable" in r.message] == []


def test_empty_and_malformed_payloads_do_not_raise() -> None:
    assert _connectivity_map({}) == {}
    assert _connectivity_map({"buildings": None, "guestBuildings": None}) == {}
    assert _connectivity_map({"buildings": ["not-a-dict"]}) == {}
    assert _connectivity_map(_payload(["not-a-dict"])) == {}
    assert _connectivity_map({"buildings": [{"id": "b1"}]}) == {}
