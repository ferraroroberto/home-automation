"""Unit tests for the multi-sub-array PV forecast (issue #555).

Nothing here touches the network — the fetch path uses a fake aiohttp session
that serves a per-sub-array GTI response keyed by (tilt, azimuth), mirroring
the pattern in ``tests/test_travel_time.py``. These pin: two sub-arrays being
requested concurrently and summed correctly, and one sub-array's failure
sinking the whole forecast (never a silently-undercounted partial result).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Dict, List, Tuple

import pytest

import src.pv_forecast as pf
from src.location_config import LocationConfig
from src.pv_system_config import PvArray, PvSystemConfig

_LOCATION = LocationConfig(lat=41.4, lon=2.1)
_TODAY = date(2026, 7, 20)


def _hourly_payload(hourly_watts: Dict[str, float]) -> dict:
    times = list(hourly_watts.keys())
    values = list(hourly_watts.values())
    return {"hourly": {"time": times, "global_tilted_irradiance": values}}


def _stamps(day: date, hours: List[int]) -> List[str]:
    return [f"{day.isoformat()}T{h:02d}:00" for h in hours]


class _FakeResp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> object:
        return self._payload


class _FakeSession:
    """Serves a GTI payload keyed by the request's (tilt, azimuth)."""

    def __init__(self, payloads_by_orientation: Dict[Tuple[float, float], object]) -> None:
        self._payloads = payloads_by_orientation
        self.requested: List[Tuple[float, float]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def get(self, url: str, *, params: dict):
        key = (params["tilt"], params["azimuth"])
        self.requested.append(key)
        return _FakeResp(self._payloads[key])


class _FailingSession(_FakeSession):
    def get(self, url: str, *, params: dict):
        key = (params["tilt"], params["azimuth"])
        self.requested.append(key)
        if key == (15, 180):
            raise RuntimeError("boom")
        return _FakeResp(self._payloads[key])


def _patch_session(monkeypatch: pytest.MonkeyPatch, session) -> None:
    monkeypatch.setattr(pf.aiohttp, "ClientSession", lambda **_kw: session)


def test_two_sub_arrays_are_summed_per_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    south = PvArray(kwp=8.0, tilt_deg=15, azimuth_deg=0)
    north = PvArray(kwp=2.0, tilt_deg=15, azimuth_deg=180)
    system = PvSystemConfig(arrays=[south, north], performance_ratio=1.0)

    payloads = {
        (15, 0): _hourly_payload({s: 1000.0 for s in _stamps(_TODAY, [12])}),
        (15, 180): _hourly_payload({s: 100.0 for s in _stamps(_TODAY, [12])}),
    }
    session = _FakeSession(payloads)
    _patch_session(monkeypatch, session)

    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=system, location=_LOCATION, today=_TODAY)
    )

    assert result.available is True
    assert sorted(session.requested) == [(15, 0), (15, 180)]
    # south: 8.0 * (1000/1000) * 1.0 = 8000 Wh; north: 2.0 * (100/1000) * 1.0 = 200 Wh
    assert result.expected == [{"hour": 12, "wh": 8200.0}]
    assert result.expected_total_wh == 8200.0
    assert result.system["total_kwp"] == 10.0
    assert len(result.system["arrays"]) == 2


def test_single_legacy_array_matches_pre_multi_array_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = PvSystemConfig(arrays=[PvArray(kwp=5.0, tilt_deg=30, azimuth_deg=0)], performance_ratio=0.8)
    payloads = {(30, 0): _hourly_payload({s: 500.0 for s in _stamps(_TODAY, [10])})}
    session = _FakeSession(payloads)
    _patch_session(monkeypatch, session)

    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=system, location=_LOCATION, today=_TODAY)
    )

    assert result.available is True
    # 5.0 * (500/1000) * 0.8 = 2000 Wh
    assert result.expected == [{"hour": 10, "wh": 2000.0}]


def test_one_sub_array_failing_sinks_the_whole_forecast(monkeypatch: pytest.MonkeyPatch) -> None:
    south = PvArray(kwp=8.0, tilt_deg=15, azimuth_deg=0)
    north = PvArray(kwp=2.0, tilt_deg=15, azimuth_deg=180)
    system = PvSystemConfig(arrays=[south, north], performance_ratio=1.0)

    payloads = {(15, 0): _hourly_payload({s: 1000.0 for s in _stamps(_TODAY, [12])})}
    session = _FailingSession(payloads)
    _patch_session(monkeypatch, session)

    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=system, location=_LOCATION, today=_TODAY)
    )

    assert result.available is False
    assert result.reason == "unreachable"


def test_no_system_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "load_pv_system_config", lambda: None)
    result = asyncio.run(pf.fetch_pv_forecast("today", location=_LOCATION))
    assert result.available is False
    assert result.reason == "not_configured"
