"""``GET /api/weather`` answers from an in-memory snapshot (#772).

Open-Meteo is faked at the ``aiohttp`` session, so the test counts real upstream
round-trips: before #772 every request made one, putting the upstream's latency
(its p95 tail) inside the request.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.location_config import LocationConfig

_PAYLOAD: Dict[str, Any] = {
    "current": {"temperature_2m": 21.5, "weather_code": 3, "is_day": 1},
    "daily": {"temperature_2m_max": [24.0], "temperature_2m_min": [15.0], "weather_code": [2]},
}


class _Upstream:
    """A stand-in for ``aiohttp.ClientSession`` that records its calls."""

    def __init__(self) -> None:
        self.calls: List[int] = []
        self.fail = False

    def session(self, *_a: Any, **_k: Any) -> "_Upstream":
        return self

    async def __aenter__(self) -> "_Upstream":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def get(self, *_a: Any, **_k: Any) -> "_Upstream":
        self.calls.append(1)
        return self

    def raise_for_status(self) -> None:
        if self.fail:
            raise RuntimeError("open-meteo down")

    async def json(self) -> Dict[str, Any]:
        return _PAYLOAD


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> _Upstream:
    fake = _Upstream()
    monkeypatch.setattr("app.webapp.routers.weather.aiohttp.ClientSession", fake.session)
    monkeypatch.setattr(
        "app.webapp.routers.weather.load_location_config",
        lambda: LocationConfig(41.0, 2.0, "Home"),
    )
    return fake


def test_repeated_reads_share_one_upstream_fetch_and_carry_their_age(
    client: TestClient, upstream: _Upstream
) -> None:
    client.get("/api/weather")
    body = client.get("/api/weather").json()
    assert len(upstream.calls) == 1
    assert body["available"] is True and body["temperature_c"] == 21.5
    assert body["temp_max_c"] == 24.0 and body["forecast_code"] == 2
    assert set(body["snapshot"]) == {"built_at", "age_seconds", "stale", "error"}


def test_a_failed_first_read_is_not_cached(client: TestClient, upstream: _Upstream) -> None:
    upstream.fail = True
    assert client.get("/api/weather").json() == {"available": False, "reason": "unreachable"}
    upstream.fail = False
    assert client.get("/api/weather").json()["available"] is True


def test_the_last_reading_survives_an_upstream_outage(
    client: TestClient, upstream: _Upstream
) -> None:
    from app.webapp.routers import weather

    client.get("/api/weather")
    upstream.fail = True
    weather.WEATHER_SNAPSHOT._built -= weather.WEATHER_SNAPSHOT.max_age_s + 1  # past the bound
    body = client.get("/api/weather").json()  # answers at once, refreshes behind
    assert body["available"] is True and body["temperature_c"] == 21.5


def test_a_missing_location_is_unavailable_not_an_error(
    client: TestClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.webapp.routers.weather.load_location_config", lambda: None)
    assert client.get("/api/weather").json() == {"available": False, "reason": "not_configured"}
    assert upstream.calls == []


def test_moving_the_home_refetches_on_the_next_read(
    client: TestClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/api/weather")
    monkeypatch.setattr("app.webapp.routers.presence.save_location_config", lambda _loc: None)
    assert client.put("/api/location", json={"lat": 48.8, "lon": 2.3, "label": "Paris"}).status_code == 200
    client.get("/api/weather")
    assert len(upstream.calls) == 2
