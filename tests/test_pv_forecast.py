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
from typing import Dict, List, Optional, Tuple

import pytest

import src.pv_forecast as pf
from src.location_config import LocationConfig
from src.pv_system_config import PvArray, PvSystemConfig

_LOCATION = LocationConfig(lat=41.4, lon=2.1)
_TODAY = date(2026, 7, 20)


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
