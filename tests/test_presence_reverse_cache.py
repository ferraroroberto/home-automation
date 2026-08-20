"""The Nominatim reverse-geocode cache must stay bounded (#667).

The webapp runs for weeks between tray restarts and the cache is keyed on
coordinates rounded to ~11 m, so an unbounded dict grows for as long as any
phone keeps moving.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from app.webapp.routers import presence


class _FakeResponse:
    def __init__(self, display_name: str) -> None:
        self.status = 200
        self._display_name = display_name

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def json(self) -> Dict[str, Any]:
        return {"display_name": self._display_name}


class _FakeSession:
    def __init__(self, calls: List[Dict[str, Any]], **_kwargs: Any) -> None:
        self._calls = calls

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def get(self, _url: str, params: Dict[str, Any], timeout: int) -> _FakeResponse:
        self._calls.append(params)
        return _FakeResponse(f"Street {params['lat']}, City")


@pytest.fixture
def fetches(monkeypatch) -> List[Dict[str, Any]]:
    """Stub Nominatim and hand back the list of outbound lookups."""

    calls: List[Dict[str, Any]] = []
    presence._REVERSE_CACHE.clear()
    monkeypatch.setattr(
        presence.aiohttp, "ClientSession", lambda **kwargs: _FakeSession(calls, **kwargs)
    )
    yield calls
    presence._REVERSE_CACHE.clear()


def test_reverse_cache_is_strictly_bounded(monkeypatch, fetches) -> None:
    monkeypatch.setattr(presence, "REVERSE_CACHE_LIMIT", 3)

    for step in range(4):
        asyncio.run(presence._reverse_geocode(41.0 + step / 100, 2.0))

    assert len(presence._REVERSE_CACHE) == 3
    assert "41.0000,2.0000" not in presence._REVERSE_CACHE
    assert "41.0300,2.0000" in presence._REVERSE_CACHE


def test_reverse_cache_evicts_least_recently_used(monkeypatch, fetches) -> None:
    monkeypatch.setattr(presence, "REVERSE_CACHE_LIMIT", 2)

    asyncio.run(presence._reverse_geocode(41.0, 2.0))  # home
    asyncio.run(presence._reverse_geocode(41.1, 2.0))  # one-off
    asyncio.run(presence._reverse_geocode(41.0, 2.0))  # home again — cache hit
    assert len(fetches) == 2, "a cached coordinate must not hit Nominatim again"

    asyncio.run(presence._reverse_geocode(41.2, 2.0))

    assert set(presence._REVERSE_CACHE) == {"41.0000,2.0000", "41.2000,2.0000"}
