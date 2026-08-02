"""Unit tests for the multi-sub-array PV forecast (issue #555).

Nothing here touches the network — the fetch path uses a fake aiohttp session
that serves a per-sub-array GTI response keyed by (tilt, azimuth), mirroring
the pattern in ``tests/test_travel_time.py``. These pin: two sub-arrays being
requested concurrently and summed correctly, and one sub-array's failure
sinking the whole forecast (never a silently-undercounted partial result).

Also covers the upstream-caching/429/backoff fix (issue #597): the cache,
shared session and failure backoff all live in module state, so an autouse
fixture resets them between tests — the same pattern ``test_huawei_client.py``
uses for its own module-state backoff.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

import aiohttp
import pytest

import src.pv_forecast as pf
from src.location_config import LocationConfig
from src.pv_system_config import PvArray, PvHorizonPoint, PvSystemConfig
from src.sun_position import SunPosition

_LOCATION = LocationConfig(lat=41.4, lon=2.1)
_TODAY = date(2026, 7, 20)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """The cache, shared session and 429 backoff live in module state — don't
    leak them between tests."""
    pf._forecast_cache.clear()
    pf._session = None
    pf._failure_until = 0.0
    pf._failure_streak = 0
    yield
    pf._forecast_cache.clear()
    pf._session = None
    pf._failure_until = 0.0
    pf._failure_streak = 0


def _hourly_payload(
    hourly_watts: Dict[str, float],
    air_c: Optional[Dict[str, Optional[float]]] = None,
) -> dict:
    times = list(hourly_watts.keys())
    values = list(hourly_watts.values())
    hourly: Dict[str, object] = {"time": times, "global_tilted_irradiance": values}
    if air_c is not None:
        hourly["temperature_2m"] = [air_c[t] for t in times]
    return {"hourly": hourly}


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
    """Serves a GTI payload keyed by the request's (tilt, azimuth).

    Not used as a context manager any more (issue #597 — the fetch path now
    reuses one long-lived session via ``_get_session()`` instead of opening
    and closing one per call), just ``.closed`` for the reuse check itself.
    """

    closed = False

    def __init__(self, payloads_by_orientation: Dict[Tuple[float, float], object]) -> None:
        self._payloads = payloads_by_orientation
        self.requested: List[Tuple[float, float]] = []

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


class _RateLimitedSession(_FakeSession):
    """Every request 429s, the way Open-Meteo does once it starts throttling."""

    class _ReqInfo:
        real_url = "https://api.open-meteo.com/v1/forecast"

    def get(self, url: str, *, params: dict):
        key = (params["tilt"], params["azimuth"])
        self.requested.append(key)
        raise aiohttp.ClientResponseError(
            self._ReqInfo(), (), status=429, message="Too Many Requests"
        )


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


# ------------------------------- arbitrary-date reach for the diagnostic (#590)
class _RecordingSession(_FakeSession):
    """Like ``_FakeSession`` but keeps the full params of every request."""

    def __init__(self, payloads_by_orientation) -> None:
        super().__init__(payloads_by_orientation)
        self.params: List[dict] = []

    def get(self, url: str, *, params: dict):
        self.params.append(dict(params))
        return super().get(url, params=params)


def _recording(monkeypatch: pytest.MonkeyPatch, day: date) -> _RecordingSession:
    payloads = {(30, 0): _hourly_payload({s: 500.0 for s in _stamps(day, [10])})}
    session = _RecordingSession(payloads)
    _patch_session(monkeypatch, session)
    return session


_ONE_ARRAY = PvSystemConfig(
    arrays=[PvArray(kwp=5.0, tilt_deg=30, azimuth_deg=0)], performance_ratio=0.8
)


@pytest.mark.parametrize("day", ["yesterday", "today", "tomorrow"])
def test_the_named_days_still_request_exactly_one_past_day(
    monkeypatch: pytest.MonkeyPatch, day: str
) -> None:
    """The forecast card's request must not drift because a diagnostic exists.

    ``past_days`` became a parameter so the sun-position overlay (#590) can
    reach further back. All three named days have to keep resolving to the
    original window, or the card's answer could change on the back of a
    read-only feature.
    """
    session = _recording(monkeypatch, _TODAY)
    asyncio.run(
        pf.fetch_pv_forecast(day, system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )
    assert len(session.params) == 1
    assert session.params[0]["past_days"] == 1
    assert session.params[0]["forecast_days"] == 2


def test_an_older_date_widens_the_lookback_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = date(2026, 7, 10)   # ten days before _TODAY
    session = _recording(monkeypatch, target)
    result = asyncio.run(
        pf.fetch_pv_forecast_for_date(
            target, system=_ONE_ARRAY, location=_LOCATION, today=_TODAY
        )
    )
    assert session.params[0]["past_days"] == 10
    assert result.available is True
    assert result.day == "2026-07-10"
    assert result.expected == [{"hour": 10, "wh": 2000.0}]


def test_a_date_past_the_lookback_ceiling_is_refused_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _recording(monkeypatch, _TODAY)
    result = asyncio.run(
        pf.fetch_pv_forecast_for_date(
            date(2026, 1, 1), system=_ONE_ARRAY, location=_LOCATION, today=_TODAY
        )
    )
    assert result.available is False
    assert result.reason == "too_old"
    assert session.params == []


# --------------------------------------- panel-temperature term (issue #591)
# The load-bearing property is the *off* path: this term ships disabled and the
# forecast card's numbers — and its upstream request — must be exactly what they
# were. The on path is pinned against hand-computed NOCT arithmetic rather than
# against itself, so a change to γ/NOCT has to be a deliberate edit here too.

_THERMAL_ARRAY = PvArray(kwp=5.0, tilt_deg=30, azimuth_deg=0)


def _thermal_system(*, enabled: bool, ratio: float) -> PvSystemConfig:
    return PvSystemConfig(
        arrays=[_THERMAL_ARRAY],
        performance_ratio=ratio,
        thermal_model_enabled=enabled,
    )


def _hot_noon(with_air: bool = True) -> dict:
    """One hour: 800 W/m² of GTI at 30 °C ambient — the NOCT reference irradiance."""
    stamps = _stamps(_TODAY, [12])
    return _hourly_payload(
        {s: 800.0 for s in stamps},
        {s: 30.0 for s in stamps} if with_air else None,
    )


def _thermal_session(monkeypatch: pytest.MonkeyPatch, payload: dict) -> _RecordingSession:
    session = _RecordingSession({(30, 0): payload})
    _patch_session(monkeypatch, session)
    return session


def test_cell_temperature_and_derate_match_the_pvwatts_arithmetic() -> None:
    """T_cell = T_air + (45−20)/800·GTI; factor = 1 − 0.0035·(T_cell − 25)."""
    assert pf.cell_temperature_c(30.0, 800.0) == pytest.approx(55.0)
    assert pf.thermal_derate(30.0, 800.0) == pytest.approx(0.895)
    # At STC cell temperature the term is a no-op, by construction.
    assert pf.cell_temperature_c(25.0, 0.0) == pytest.approx(25.0)
    assert pf.thermal_derate(25.0, 0.0) == pytest.approx(1.0)
    # A cold, bright hour gains rather than loses.
    assert pf.thermal_derate(0.0, 800.0) == pytest.approx(1.0)
    assert pf.thermal_derate(-10.0, 400.0) > 1.0


def test_switch_off_changes_neither_the_request_nor_the_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path must be byte-for-byte the pre-#591 one.

    Ambient temperature is present in the response and still ignored, and the
    request never asks for it — the disabled feature cannot drift the card.
    """
    session = _thermal_session(monkeypatch, _hot_noon())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_thermal_system(enabled=False, ratio=0.8),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert session.params[0]["hourly"] == "global_tilted_irradiance"
    # 5.0 kWp × (800/1000) × 0.8 = 3200 Wh — unchanged by the 30 °C ambient.
    assert result.expected == [{"hour": 12, "wh": 3200.0}]
    assert "thermal_model" not in result.system


def test_switch_on_asks_for_temperature_in_the_same_request_and_derates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _thermal_session(monkeypatch, _hot_noon())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_thermal_system(enabled=True, ratio=0.88),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    # One request, not two: temperature rides along with GTI.
    assert len(session.params) == 1
    assert session.params[0]["hourly"] == "global_tilted_irradiance,temperature_2m"
    # 5.0 × 0.8 × 0.88 kWh = 3520 Wh, × 0.895 thermal factor = 3150.4 Wh.
    assert result.expected[0]["hour"] == 12
    assert result.expected[0]["wh"] == pytest.approx(3150.4)
    assert result.system["thermal_model"] == {
        "gamma_per_c": pf.THERMAL_GAMMA_PER_C,
        "noct_c": pf.THERMAL_NOCT_C,
    }


def test_the_half_migrated_combination_refuses_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Term on + the un-migrated 0.80 combined ratio would double-count the
    thermal loss, so it must produce no curve at all rather than one that is
    quietly ~10% low."""
    session = _thermal_session(monkeypatch, _hot_noon())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_thermal_system(enabled=True, ratio=0.8),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.available is False
    assert result.reason == "thermal_ratio_unmigrated"
    assert session.params == []


def test_switch_on_without_temperature_in_the_response_is_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently falling back to the un-derated curve would be the same
    double-counting bug wearing an upstream outage as a disguise."""
    _thermal_session(monkeypatch, _hot_noon(with_air=False))
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_thermal_system(enabled=True, ratio=0.88),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.available is False
    assert result.reason == "no_data"


def test_a_single_null_ambient_hour_falls_back_to_no_derate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamps = _stamps(_TODAY, [11, 12])
    payload = _hourly_payload(
        {stamps[0]: 800.0, stamps[1]: 800.0},
        {stamps[0]: None, stamps[1]: 30.0},
    )
    _thermal_session(monkeypatch, payload)
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_thermal_system(enabled=True, ratio=0.88),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.expected[0]["wh"] == pytest.approx(3520.0)   # no ambient → no derate
    assert result.expected[1]["wh"] == pytest.approx(3150.4)


# ------------------------------------------ horizon / shading (issue #578 part b)
# Same shape as the thermal-term tests above: the off path must be byte-for-
# byte identical to before, and the on path is pinned against hand-computed
# arithmetic — a fixed sun position (via monkeypatch, so the test doesn't
# depend on getting real solar geometry right) and a known diffuse fraction.

_HORIZON_ARRAY = PvArray(kwp=5.0, tilt_deg=30, azimuth_deg=0)


def _horizon_system(*, enabled: bool, profile=None) -> PvSystemConfig:
    return PvSystemConfig(
        arrays=[_HORIZON_ARRAY],
        performance_ratio=0.8,
        horizon_profile_enabled=enabled,
        horizon_profile=profile or [],
    )


def _horizon_payload(
    gti: float = 800.0, direct: float = 600.0, diffuse: float = 200.0,
    include_components: bool = True,
) -> dict:
    stamps = _stamps(_TODAY, [15])
    hourly: Dict[str, object] = {"time": stamps, "global_tilted_irradiance": [gti]}
    if include_components:
        hourly["direct_radiation"] = [direct]
        hourly["diffuse_radiation"] = [diffuse]
    return {"hourly": hourly, "utc_offset_seconds": 0}


def _horizon_session(monkeypatch: pytest.MonkeyPatch, payload: dict) -> _RecordingSession:
    session = _RecordingSession({(30, 0): payload})
    _patch_session(monkeypatch, session)
    return session


def test_horizon_switch_off_changes_neither_the_request_nor_the_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _horizon_session(monkeypatch, _horizon_payload())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=False),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert session.params[0]["hourly"] == "global_tilted_irradiance"
    # 5.0 kWp × (800/1000) × 0.8 = 3200 Wh — unaffected by direct/diffuse present
    # in the response but never requested for a real (non-test) call.
    assert result.expected == [{"hour": 15, "wh": 3200.0}]


def test_horizon_switch_on_asks_for_components_in_the_same_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _horizon_session(monkeypatch, _horizon_payload())
    asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=True, profile=[PvHorizonPoint(90, 10)]),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    # One request, not two or three.
    assert len(session.params) == 1
    assert session.params[0]["hourly"] == (
        "global_tilted_irradiance,direct_radiation,diffuse_radiation"
    )


def test_sun_above_the_horizon_point_is_unshaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pf, "sun_position",
        lambda ts, lat, lon: SunPosition(azimuth_deg=180.0, elevation_deg=40.0),
    )
    _horizon_session(monkeypatch, _horizon_payload())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=True, profile=[PvHorizonPoint(180, 10)]),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.expected == [{"hour": 15, "wh": 3200.0}]


def test_sun_below_the_horizon_point_keeps_only_the_diffuse_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pf, "sun_position",
        lambda ts, lat, lon: SunPosition(azimuth_deg=180.0, elevation_deg=5.0),
    )
    _horizon_session(monkeypatch, _horizon_payload(direct=600.0, diffuse=200.0))
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=True, profile=[PvHorizonPoint(180, 10)]),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    # diffuse fraction = 200/(600+200) = 0.25; 3200 Wh × 0.25 = 800 Wh.
    assert result.expected == [{"hour": 15, "wh": 800.0}]


def test_empty_profile_with_switch_on_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch on + no points entered yet must not zero the evening."""
    monkeypatch.setattr(
        pf, "sun_position",
        lambda ts, lat, lon: SunPosition(azimuth_deg=180.0, elevation_deg=-5.0),
    )
    _horizon_session(monkeypatch, _horizon_payload())
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=True, profile=[]),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.expected == [{"hour": 15, "wh": 3200.0}]


def test_horizon_switch_on_without_components_in_the_response_is_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _horizon_session(monkeypatch, _horizon_payload(include_components=False))
    result = asyncio.run(
        pf.fetch_pv_forecast(
            "today",
            system=_horizon_system(enabled=True, profile=[PvHorizonPoint(90, 10)]),
            location=_LOCATION,
            today=_TODAY,
        )
    )
    assert result.available is False
    assert result.reason == "no_data"


def test_horizon_elevation_deg_interpolates_and_wraps() -> None:
    profile = [PvHorizonPoint(0, 0), PvHorizonPoint(90, 20), PvHorizonPoint(270, 10)]
    assert pf.horizon_elevation_deg(profile, 45) == pytest.approx(10.0)
    assert pf.horizon_elevation_deg(profile, 315) == pytest.approx(5.0)  # wraps 270→0
    assert pf.horizon_elevation_deg([], 45) == -90.0
    assert pf.horizon_elevation_deg([PvHorizonPoint(50, 15)], 300) == 15.0


def test_diffuse_fraction_handles_the_zero_light_edge_case() -> None:
    assert pf._diffuse_fraction(600.0, 200.0) == pytest.approx(0.25)
    assert pf._diffuse_fraction(0.0, 0.0) == 1.0


# --------------------------------------------- caching / 429 / backoff (#597)
# A per-render Open-Meteo request, an unhandled 429, and no backoff turned a
# batch of e2e/backend runs into a self-sustaining rate limit. These pin: one
# upstream request set per cache TTL window, a 429 surfacing as its own
# ``rate_limited`` reason with a backoff opened, and the last good curve being
# served — never a blank card — both while an existing backoff is active and
# when a live refetch itself 429s.


def test_repeated_renders_within_ttl_reuse_the_cached_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _recording(monkeypatch, _TODAY)
    for _ in range(5):
        result = asyncio.run(
            pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
        )
        assert result.available is True
        assert result.expected == [{"hour": 10, "wh": 2000.0}]
    assert len(session.params) == 1


def test_session_is_reused_across_independent_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fetches whose cache keys differ (different array config) must not
    open two connection pools — issue #597's second defect."""
    session = _FakeSession({(30, 0): _hourly_payload({s: 500.0 for s in _stamps(_TODAY, [10])})})
    calls = {"n": 0}

    def _factory(**_kw):
        calls["n"] += 1
        return session

    monkeypatch.setattr(pf.aiohttp, "ClientSession", _factory)

    system_a = PvSystemConfig(arrays=[PvArray(kwp=5.0, tilt_deg=30, azimuth_deg=0)], performance_ratio=0.8)
    system_b = PvSystemConfig(arrays=[PvArray(kwp=6.0, tilt_deg=30, azimuth_deg=0)], performance_ratio=0.8)

    r1 = asyncio.run(pf.fetch_pv_forecast("today", system=system_a, location=_LOCATION, today=_TODAY))
    r2 = asyncio.run(pf.fetch_pv_forecast("today", system=system_b, location=_LOCATION, today=_TODAY))

    assert r1.available is True and r2.available is True
    assert calls["n"] == 1  # one aiohttp.ClientSession() call for both fetches


def test_a_429_is_reported_as_rate_limited_and_opens_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RateLimitedSession({})
    _patch_session(monkeypatch, session)

    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )

    assert result.available is False
    assert result.reason == "rate_limited"
    assert result.reason != "unreachable"
    assert pf._failure_streak == 1
    assert pf._failure_until > 0.0


def test_while_an_existing_backoff_is_active_the_last_good_curve_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _recording(monkeypatch, _TODAY)
    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )
    assert result.available is True
    assert len(good.params) == 1

    # Age the cache past its TTL, then open a backoff window as an earlier
    # failed render would have — a fresh render now must not touch the network.
    key = pf._cache_key(_TODAY, _ONE_ARRAY, _LOCATION)
    ts, cached = pf._forecast_cache[key]
    pf._forecast_cache[key] = (ts - pf._CACHE_TTL_S - 1, cached)
    pf._failure_until = time.monotonic() + 60
    pf._failure_streak = 1

    limited = _RateLimitedSession({})
    _patch_session(monkeypatch, limited)

    served = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )

    assert served.available is True
    assert served.expected == result.expected
    assert limited.requested == []  # backing off: no upstream call at all


def test_a_429_on_a_live_refetch_still_serves_the_stale_cached_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _recording(monkeypatch, _TODAY)
    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )
    assert result.available is True

    # Age the cache past its TTL (but well within the stale-serve window) so
    # the next render attempts a live refetch instead of a plain cache hit.
    key = pf._cache_key(_TODAY, _ONE_ARRAY, _LOCATION)
    ts, cached = pf._forecast_cache[key]
    pf._forecast_cache[key] = (ts - pf._CACHE_TTL_S - 1, cached)

    # The real session survives across renders (that's issue #597's fix); for
    # this test we swap in a session that starts 429ing, the way the real one
    # would once Open-Meteo starts throttling it.
    pf._session = None
    limited = _RateLimitedSession({})
    _patch_session(monkeypatch, limited)

    served = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )

    assert served.available is True
    assert served.expected == result.expected
    assert limited.requested == [(30, 0)]  # a live attempt was made, and it 429'd
    assert pf._failure_streak == 1


def test_a_429_with_no_cache_at_all_reports_rate_limited_without_a_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited = _RateLimitedSession({})
    _patch_session(monkeypatch, limited)

    result = asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )

    assert result.available is False
    assert result.reason == "rate_limited"


def test_a_non_429_failure_does_not_open_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic network failure stays ``unreachable`` and must not be
    conflated with the rate-limit backoff — a real outage would then be
    invisible behind a 429's stale-cache fallback."""
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
    assert pf._failure_streak == 0
    assert pf._failure_until == 0.0


def test_backoff_doubles_per_streak_and_success_clears_it() -> None:
    pf._note_failure(1000.0)
    assert pf._failure_streak == 1
    assert pf._failure_until == 1000.0 + pf._FAILURE_BACKOFF_BASE_S

    pf._note_failure(pf._failure_until)
    assert pf._failure_streak == 2
    assert pf._failure_until == pytest.approx(
        1000.0 + pf._FAILURE_BACKOFF_BASE_S + pf._FAILURE_BACKOFF_BASE_S * 2
    )

    pf._note_success()
    assert pf._failure_streak == 0
    assert pf._failure_until == 0.0


def test_a_successful_fetch_prunes_cache_entries_past_the_stale_serve_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, the cache would grow by one entry per calendar day for as
    long as the tray stays up (``target_day`` rolls forward daily)."""
    session = _recording(monkeypatch, _TODAY)
    asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )
    key = pf._cache_key(_TODAY, _ONE_ARRAY, _LOCATION)
    ts, cached = pf._forecast_cache[key]
    pf._forecast_cache[key] = (ts - pf._STALE_SERVE_MAX_S - 1, cached)
    # A second, unrelated day so there's a fresh entry to write and trigger pruning.
    pf._forecast_cache[("stale-sentinel",)] = (ts - pf._STALE_SERVE_MAX_S - 1, cached)

    asyncio.run(
        pf.fetch_pv_forecast("today", system=_ONE_ARRAY, location=_LOCATION, today=_TODAY)
    )

    assert ("stale-sentinel",) not in pf._forecast_cache
    assert key in pf._forecast_cache  # the entry this render just wrote


def test_the_existing_reasons_are_unaffected_by_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not_configured / no_location / too_old / no_data still behave exactly
    as before — the cache/backoff wrapper only sits around the network call."""
    monkeypatch.setattr(pf, "load_pv_system_config", lambda: None)
    assert asyncio.run(pf.fetch_pv_forecast("today", location=_LOCATION)).reason == "not_configured"

    result = asyncio.run(
        pf.fetch_pv_forecast_for_date(
            date(2026, 1, 1), system=_ONE_ARRAY, location=_LOCATION, today=_TODAY
        )
    )
    assert result.reason == "too_old"
