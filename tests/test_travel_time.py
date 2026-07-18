"""Pure-logic + fetch-path tests for the Google Routes travel-time client (#470/#472).

Nothing here touches the network — the fetch path uses a fake aiohttp session.
The endpoint-level behaviour (resolution, home lookup, spoken fallbacks) is
covered by ``tests/api/test_presence_eta.py`` with ``fetch_travel_time``
monkeypatched.
"""

from __future__ import annotations

import asyncio

import pytest

import src.travel_time as tt
from src.travel_time import ROUTES_URL, TravelTime, _parse_routes, fetch_travel_time


def test_parse_routes_reads_duration_string() -> None:
    result = _parse_routes({"routes": [{"duration": "1080s"}]})
    assert result.available is True
    assert result.duration_s == 1080
    assert result.duration_text == "1080s"


def test_parse_routes_empty_routes_is_no_route() -> None:
    result = _parse_routes({"routes": []})
    assert result.available is False
    assert result.reason == "no_route"


def test_parse_routes_missing_duration_is_error_not_raise() -> None:
    assert _parse_routes({"routes": [{"distanceMeters": 5000}]}).reason == "error"
    assert _parse_routes("nope").reason == "error"


# --------------------------------------------------------------- fetch path
class _FakeResp:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, status: int, payload: object, capture: dict) -> None:
        self._status = status
        self._payload = payload
        self._capture = capture

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def post(self, url: str, *, json: object = None, headers: object = None, timeout: object = None):
        self._capture.update(url=url, json=json, headers=headers)
        return _FakeResp(self._status, self._payload)


def _patch_session(monkeypatch: pytest.MonkeyPatch, status: int, payload: object) -> dict:
    capture: dict = {}
    monkeypatch.setattr(tt, "_api_key", lambda: "test-key")
    monkeypatch.setattr(tt.aiohttp, "ClientSession", lambda: _FakeSession(status, payload, capture))
    return capture


def test_fetch_success_posts_routes_request_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _patch_session(monkeypatch, 200, {"routes": [{"duration": "1080s"}]})

    result = asyncio.run(
        fetch_travel_time(origin_lat=41.48, origin_lon=2.06, dest_lat=41.40, dest_lon=2.15)
    )
    assert result.available is True
    assert result.duration_s == 1080
    # Correct endpoint, key header, traffic-aware body.
    assert capture["url"] == ROUTES_URL
    assert capture["headers"]["X-Goog-Api-Key"] == "test-key"
    assert capture["headers"]["X-Goog-FieldMask"] == "routes.duration"
    assert capture["json"]["routingPreference"] == "TRAFFIC_AWARE"
    assert capture["json"]["origin"]["location"]["latLng"]["latitude"] == 41.48


def test_fetch_api_not_enabled_403_is_error_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(
        monkeypatch, 403, {"error": {"message": "Routes API has not been used in project ..."}}
    )
    result = asyncio.run(
        fetch_travel_time(origin_lat=1.0, origin_lon=2.0, dest_lat=3.0, dest_lon=4.0)
    )
    assert result.available is False
    assert result.reason == "error"
    assert "Routes API" in result.detail


def test_fetch_without_api_key_is_unavailable_and_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing key short-circuits before any HTTP session is created."""
    monkeypatch.setattr(tt, "_api_key", lambda: "")

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("no HTTP call should happen without an API key")

    monkeypatch.setattr(tt.aiohttp, "ClientSession", _boom)

    result = asyncio.run(
        fetch_travel_time(origin_lat=1.0, origin_lon=2.0, dest_lat=3.0, dest_lon=4.0)
    )
    assert result == TravelTime(available=False, reason="no_api_key")
