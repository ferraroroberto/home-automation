"""Tesla client: read-only by construction, never wakes the car, never raises."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src._backoff import BackoffTracker
from src.devices import tesla_client as tc
from src.location_config import LocationConfig

ONLINE_DATA = {
    "response": {
        "charge_state": {
            "battery_level": 64, "battery_range": 200.0, "charging_state": "Charging",
            "charger_power": 7, "charge_energy_added": 3.25,
        },
        "drive_state": {"shift_state": None, "speed": None, "latitude": 40.0001, "longitude": -3.0001},
        "vehicle_state": {"odometer": 10_000.0},
    }
}


class _FakeResponse:
    def __init__(self, status: int, body: Dict[str, Any]) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: Any = None) -> Dict[str, Any]:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def fake_api(monkeypatch):
    """Route the client's GETs to canned responses (longest prefix wins), recording each path."""
    calls: List[str] = []
    routes: Dict[str, _FakeResponse] = {}

    async def fake_get(session, config, path):
        calls.append(path)
        matches = [prefix for prefix in routes if path.startswith(prefix)]
        if not matches:
            raise AssertionError(f"unexpected path {path}")
        return routes[max(matches, key=len)]

    monkeypatch.setattr(tc, "_get", fake_get)
    monkeypatch.setattr(tc, "is_configured", lambda config: True)
    monkeypatch.setattr(tc, "_vehicle_id", None)
    monkeypatch.setattr(tc, "_backoff", BackoffTracker(base_s=60.0, max_s=1800.0))
    monkeypatch.setattr(tc, "load_location_config", lambda: LocationConfig(lat=40.0, lon=-3.0))
    routes["/api/1/vehicles/"] = _FakeResponse(200, {"response": {"state": "asleep"}})
    routes["/api/1/vehicles"] = _FakeResponse(200, {"response": [{"id_s": "42", "vin": "X"}]})
    return calls, routes


def test_client_exposes_read_methods_only() -> None:
    public = {
        name for name, obj in inspect.getmembers(tc, inspect.isfunction)
        if obj.__module__ == tc.__name__ and not name.startswith("_")
    }
    assert public == {"parse_vehicle_data", "read_vehicle"}

    source = Path(tc.__file__).read_text(encoding="utf-8")
    for forbidden in (".post(", ".put(", ".patch(", ".delete(", "wake_up", "/command/"):
        assert forbidden not in source, forbidden


def test_asleep_car_is_not_woken_and_yields_no_sample(fake_api) -> None:
    calls, routes = fake_api
    routes["/api/1/vehicles/42/vehicle_data"] = _FakeResponse(200, ONLINE_DATA)

    reading = asyncio.run(tc.read_vehicle())

    assert reading.state == "asleep" and reading.sample is None
    assert not any("vehicle_data" in c or "wake" in c for c in calls)


def test_online_car_yields_a_metric_sample_without_coordinates(fake_api) -> None:
    _, routes = fake_api
    routes["/api/1/vehicles/42/vehicle_data"] = _FakeResponse(200, ONLINE_DATA)
    routes["/api/1/vehicles/"] = _FakeResponse(200, {"response": {"state": "online"}})

    reading = asyncio.run(tc.read_vehicle())

    sample = reading.sample
    assert reading.state == "online" and sample is not None
    assert sample.battery_pct == 64 and sample.charging
    assert sample.range_km == pytest.approx(321.87, abs=0.01)
    assert sample.odometer_km == pytest.approx(16_093.44)
    assert sample.at_home is True
    assert not {"latitude", "longitude"} & set(sample.to_dict())


def test_state_only_read_skips_vehicle_data(fake_api) -> None:
    calls, routes = fake_api
    routes["/api/1/vehicles/"] = _FakeResponse(200, {"response": {"state": "online"}})
    reading = asyncio.run(tc.read_vehicle(with_data=False))
    assert reading.state == "online" and reading.sample is None
    assert not any("vehicle_data" in c for c in calls)


def test_unconfigured_makes_no_request(monkeypatch) -> None:
    monkeypatch.setattr(tc, "is_configured", lambda config: False)
    monkeypatch.setattr(tc, "_get", lambda *a: pytest.fail("no request expected"))
    assert asyncio.run(tc.read_vehicle()).state == "unconfigured"


def test_failure_backs_off_instead_of_raising(fake_api) -> None:
    calls, routes = fake_api
    routes["/api/1/vehicles"] = _FakeResponse(401, {})

    first = asyncio.run(tc.read_vehicle())
    second = asyncio.run(tc.read_vehicle())

    assert first.state == "error" and "401" in (first.error or "")
    assert second.state == "error" and "backing off" in (second.error or "")
    assert len(calls) == 1


def test_known_awake_reads_data_directly_and_408_means_asleep(fake_api) -> None:
    calls, routes = fake_api
    routes["/api/1/vehicles/42/vehicle_data"] = _FakeResponse(408, {})
    reading = asyncio.run(tc.read_vehicle(known_awake=True))
    assert reading.state == "asleep" and reading.sample is None
    assert calls == ["/api/1/vehicles", calls[1]] and "vehicle_data" in calls[1]
