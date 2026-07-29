"""Pure-logic tests for the Huawei FusionSolar energy client.

No network: every test feeds :mod:`src.huawei_client` a payload shaped like the
portal's own response and asserts on the mapping. The alignment tests cover a
real defect — see :func:`test_latest_aligned_prefers_a_common_bucket`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pytest

from src import huawei_client
from src.huawei_client import (
    EnergyState,
    _apply,
    _backoff_for,
    _is_settled,
    _is_stale,
    _latest_aligned,
    _latest_pv,
    _note_failure,
    _note_success,
    _settled_buckets,
    _state_from_point,
)


_real_get_client = huawei_client._get_client


@pytest.fixture(autouse=True)
def _reset_backoff():
    """The backoff lives in module state — don't leak it between tests."""
    huawei_client._failure_until = 0.0
    huawei_client._failure_streak = 0
    huawei_client._stats_cache = None
    huawei_client._last_log_key = None
    yield
    huawei_client._failure_until = 0.0
    huawei_client._failure_streak = 0
    huawei_client._stats_cache = None
    huawei_client._last_log_key = None


def _stats(product, use, meter, *, times=None, **extra):
    """Build a plant-stats payload with parallel 5-minute series."""
    times = times or [f"2026-07-28 18:{m:02d}" for m in (0, 5, 10)]
    payload = {
        "xAxis": times,
        "productPower": product,
        "usePower": use,
        "meterActivePower": meter,
        "existMeter": True,
        "existInverter": True,
    }
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------
# Bucket alignment
# --------------------------------------------------------------------------

def test_latest_aligned_prefers_a_common_bucket():
    """A lagging series must not be mixed with fresher ones.

    Observed live: ``productPower`` last reported at 18:05 while ``usePower``
    and ``meterActivePower`` had already reached 18:10. Taking the newest
    sample of each series independently produced a snapshot that was off by
    ~200 W and did not balance. The read must fall back to the newest bucket
    where all three have a value.
    """
    stats = _stats(
        product=[1.0, 2.368, "--"],
        use=[0.9, 2.125, 2.175],
        meter=[-0.1, -0.243, -0.025],
    )
    as_of, point = _latest_aligned(stats)

    assert as_of == "2026-07-28 18:05"
    assert point == {
        "productPower": 2.368,
        "usePower": 2.125,
        "meterActivePower": -0.243,
    }
    # The whole point of aligning: the flow identity holds exactly.
    assert point["productPower"] + point["meterActivePower"] == pytest.approx(
        point["usePower"]
    )


def test_latest_aligned_ignores_a_series_absent_all_day():
    """No power sensor fitted must not block the PV/consumption read."""
    stats = _stats(
        product=[1.0, 2.0, 3.0],
        use=[0.9, 1.9, 2.9],
        meter=["--", "--", "--"],
    )
    as_of, point = _latest_aligned(stats)

    assert as_of == "2026-07-28 18:10"
    assert point == {"productPower": 3.0, "usePower": 2.9}
    assert "meterActivePower" not in point


def test_latest_aligned_steps_back_over_a_half_written_bucket():
    """The newest bucket can be partially filled without being marked ``--``.

    Observed live at 18:25: productPower 2.054, meterActivePower -2.167, and
    usePower still at a placeholder 0.000. Taking it at face value rendered the
    house at 0 W and the grid exporting 2,167 W from a 2,054 W array — more
    than was generated. Such a bucket must be skipped in favour of the last one
    that balances.
    """
    stats = _stats(
        product=[2.123, 2.054],
        use=[1.758, 0.000],
        meter=[-0.365, -2.167],
        times=["2026-07-28 18:20", "2026-07-28 18:25"],
    )
    as_of, point = _latest_aligned(stats)

    assert as_of == "2026-07-28 18:20"
    assert point == {
        "productPower": 2.123,
        "usePower": 1.758,
        "meterActivePower": -0.365,
    }


def test_a_settled_bucket_is_accepted():
    assert _is_settled(
        {"productPower": 2.123, "usePower": 1.758, "meterActivePower": -0.365}
    ) is True


def test_a_half_written_bucket_is_rejected():
    assert _is_settled(
        {"productPower": 2.054, "usePower": 0.0, "meterActivePower": -2.167}
    ) is False


def test_an_uncheckable_bucket_counts_as_settled():
    """Without a meter the identity cannot be tested, so don't discard the read."""
    assert _is_settled({"productPower": 2.0, "usePower": 1.5}) is True


def test_a_bucket_claiming_more_export_than_generation_is_rejected():
    """Physically impossible with no battery, so the bucket cannot be real.

    Live at 19:25: the meter claimed 2.013 kW of export from a 1.045 kW array.
    The identity check catches it, which is why it doubles as a plausibility
    check on the meter and not just on the derived consumption series.
    """
    assert _is_settled(
        {"productPower": 1.045, "usePower": 0.0, "meterActivePower": -2.013}
    ) is False


def test_a_marginal_placeholder_is_still_rejected():
    """Only 22 W off, but still a placeholder — the first tolerance let it pass.

    As PV falls towards the export level in the evening, the gap a zero
    placeholder leaves shrinks towards zero, so a generous window is exactly
    wrong here.
    """
    assert _is_settled(
        {"productPower": 1.121, "usePower": 0.0, "meterActivePower": -1.143}
    ) is False


def test_night_time_zeroes_are_settled():
    """All-zero is a legitimate balanced state, not a half-written bucket."""
    assert _is_settled(
        {"productPower": 0.0, "usePower": 0.0, "meterActivePower": 0.0}
    ) is True


def test_latest_aligned_handles_an_empty_day():
    assert _latest_aligned(_stats(["--"], ["--"], ["--"])) == (None, {})
    assert _latest_aligned({}) == (None, {})


# --------------------------------------------------------------------------
# Sign convention
# --------------------------------------------------------------------------

def test_positive_meter_power_is_an_import():
    """FusionSolar signs ``meterActivePower`` positive when drawing from grid.

    This is the opposite of the portal's own device page, so the direction is
    asserted explicitly rather than left to a reader's assumption.
    """
    state = EnergyState()
    _apply(state, _stats([2.660], [3.225], [0.565], times=["2026-07-28 17:50"]))

    assert state.grid_import_w == 565.0
    assert state.grid_export_w == 0.0
    assert state.pv_power_w == 2660.0
    assert state.house_consumption_w == 3225.0


def test_negative_meter_power_is_an_export():
    state = EnergyState()
    _apply(state, _stats([2.368], [2.125], [-0.243], times=["2026-07-28 18:05"]))

    assert state.grid_import_w == 0.0
    assert state.grid_export_w == 243.0


def test_applied_state_balances():
    """Solar + import − export − house must come to zero, or the tile lies."""
    state = EnergyState()
    _apply(state, _stats([2.368], [2.125], [-0.243], times=["2026-07-28 18:05"]))

    balance = (
        state.pv_power_w + state.grid_import_w
        - state.grid_export_w - state.house_consumption_w
    )
    assert balance == pytest.approx(0.0)


def test_daily_totals_and_reachability_map_across():
    state = EnergyState()
    _apply(
        state,
        _stats(
            [2.0], [2.5], [0.5],
            times=["2026-07-28 18:05"],
            totalBuyPower=1.97,
            totalOnGridPower=0.37,
        ),
    )

    assert state.grid_import_kwh == 1.97
    assert state.grid_export_kwh == 0.37
    assert state.meter_reachable is True
    assert state.inverter_reachable is True


def test_missing_devices_are_not_reachable():
    state = EnergyState()
    _apply(state, _stats(["--"], ["--"], ["--"]))

    assert state.meter_reachable is False
    assert state.inverter_reachable is False
    assert state.pv_power_w is None


# --------------------------------------------------------------------------
# Whole-day series (history backfill)
# --------------------------------------------------------------------------

def test_settled_buckets_returns_the_whole_day_oldest_first():
    """Backfill needs every good bucket, not just the newest one."""
    stats = _stats(
        product=[1.0, 2.123, 2.054],
        use=[0.8, 1.758, 0.000],
        meter=[-0.2, -0.365, -2.167],
    )
    buckets = _settled_buckets(stats)

    # The half-written 18:10 bucket is dropped; the two good ones survive in order.
    assert [when for when, _ in buckets] == [
        "2026-07-28 18:00",
        "2026-07-28 18:05",
    ]


def test_settled_buckets_skips_unfilled_slots():
    stats = _stats(
        product=[1.0, "--", 2.0],
        use=[0.8, "--", 1.6],
        meter=[-0.2, "--", -0.4],
    )
    assert [when for when, _ in _settled_buckets(stats)] == [
        "2026-07-28 18:00",
        "2026-07-28 18:10",
    ]


def test_state_from_point_maps_and_derives():
    state = _state_from_point(
        {"productPower": 2.123, "usePower": 1.758, "meterActivePower": -0.365}
    )

    assert state.pv_power_w == 2123.0
    assert state.house_consumption_w == 1758.0
    assert state.grid_export_w == 365.0
    assert state.grid_import_w == 0.0
    assert state.pv_surplus_w == 365.0
    assert state.meter_reachable is True
    assert state.inverter_reachable is True


# --------------------------------------------------------------------------
# Degraded read — power sensor faulty, inverter still fine
# --------------------------------------------------------------------------

def test_latest_pv_ignores_meter_consistency():
    """A meter fault must not cost us the inverter's own reading.

    Live on commissioning day the portal published ``load 0.000`` with 1.974 kW
    of export from a 0.771 kW array, for over half an hour. ``productPower``
    comes straight off the inverter and stayed sane throughout, so the read
    degrades to PV-only instead of blanking the tile.
    """
    stats = _stats(
        product=[1.045, 0.846, 0.771],
        use=[0.000, 0.000, 0.000],
        meter=[-2.013, -1.902, -1.974],
    )

    # Nothing is self-consistent, so there is no settled bucket at all...
    assert _settled_buckets(stats) == []
    # ...but the PV series is still perfectly readable.
    assert _latest_pv(stats) == ("2026-07-28 18:10", 0.771)


def test_latest_pv_skips_unfilled_slots():
    stats = _stats(product=[1.0, 2.0, "--"], use=[1.0, 2.0, "--"], meter=[0.0, 0.0, "--"])
    assert _latest_pv(stats) == ("2026-07-28 18:05", 2.0)


def test_latest_pv_on_an_empty_series():
    assert _latest_pv({}) == (None, None)


# --------------------------------------------------------------------------
# Failure backoff
# --------------------------------------------------------------------------

def test_repeat_state_lines_drop_to_debug(caplog):
    """The same bucket must be announced once, not once per poll.

    The PWA polls every 5 s against a 5-minute grid, so an unconditional line
    per read wrote the identical warning hundreds of times an hour and buried
    everything else in the log.
    """
    huawei_client._last_log_key = None

    with caplog.at_level(logging.WARNING, logger="huawei"):
        for _ in range(5):
            huawei_client._log_state(
                logging.WARNING, ("degraded", "18:05"), "flow unusable %s", "18:05"
            )

    assert len(caplog.records) == 1

    # A new bucket is news again.
    with caplog.at_level(logging.WARNING, logger="huawei"):
        huawei_client._log_state(
            logging.WARNING, ("degraded", "18:10"), "flow unusable %s", "18:10"
        )

    assert len(caplog.records) == 2


def test_backoff_doubles_then_caps():
    """A failed read must not be retried at poll rate.

    Nothing goes in the response cache when a read fails, so without a backoff
    every request re-attempts the login — and the PWA polls energy every 5 s.
    Observed live: an expired session became a login storm and the portal then
    refused the long-lived process outright.
    """
    assert _backoff_for(1) == 60
    assert _backoff_for(2) == 120
    assert _backoff_for(3) == 240
    # ...and it stops doubling rather than growing without bound.
    assert _backoff_for(9) == 900
    assert _backoff_for(50) == 900


def test_no_backoff_before_any_failure():
    assert _backoff_for(0) == 0


def test_a_failing_read_stops_calling_the_cloud_and_serves_the_last_payload():
    """The whole point of the backoff: one failure, then silence.

    Also proves the stale payload is still served while backing off — its own
    as-of timestamp gates freshness downstream, so a genuinely dead feed is
    reported unavailable by the staleness guard rather than by an empty read.
    """
    config = huawei_client.EnergyConfig(
        user="u", password="p", subdomain="x", plant_dn="NE=1", cache_ttl_s=0
    )
    last_good = {"xAxis": ["2026-07-28 18:00"], "productPower": [1.0]}
    huawei_client._stats_cache = (0.0, last_good)

    attempts = []

    async def _login_fails(_config):
        attempts.append("login")
        return None

    huawei_client._get_client = _login_fails
    try:
        assert asyncio.run(huawei_client._fetch_stats(config)) is None
        assert attempts == ["login"]

        # Second call, well inside the 60 s window: no cloud call at all...
        assert asyncio.run(huawei_client._fetch_stats(config)) == last_good
        assert attempts == ["login"]
    finally:
        huawei_client._get_client = _real_get_client
        huawei_client._stats_cache = None


def test_a_read_failure_drops_the_cached_client_so_the_next_try_is_fresh():
    """Reproduces #556: a client that fails past its own session check is worse
    than none, since ``_get_client`` happily reuses it forever otherwise.
    """
    config = huawei_client.EnergyConfig(
        user="u", password="p", subdomain="x", plant_dn="NE=1", cache_ttl_s=0
    )
    sentinel_client = object()
    huawei_client._client = sentinel_client
    huawei_client._plant_dn = "NE=1"

    async def _reuse_cached_client(_config):
        return sentinel_client

    def _read_plant_fails(_client, _plant_dn):
        raise RuntimeError("Failed to reset session and login again.")

    huawei_client._get_client = _reuse_cached_client
    real_read_plant = huawei_client._read_plant
    huawei_client._read_plant = _read_plant_fails
    try:
        assert asyncio.run(huawei_client._fetch_stats(config)) is None
        assert huawei_client._client is None
        assert huawei_client._plant_dn is None
    finally:
        huawei_client._get_client = _real_get_client
        huawei_client._read_plant = real_read_plant
        huawei_client._client = None
        huawei_client._plant_dn = None


def test_a_plant_dn_resolution_failure_also_drops_the_cached_client():
    """Same #556 reasoning on the other failure branch of ``_fetch_stats``."""
    config = huawei_client.EnergyConfig(
        user="u", password="p", subdomain="x", plant_dn=None, cache_ttl_s=0
    )
    sentinel_client = object()
    huawei_client._client = sentinel_client
    huawei_client._plant_dn = None

    async def _reuse_cached_client(_config):
        return sentinel_client

    async def _resolve_fails(_client, _config):
        return None

    huawei_client._get_client = _reuse_cached_client
    real_resolve = huawei_client._resolve_plant_dn
    huawei_client._resolve_plant_dn = _resolve_fails
    try:
        assert asyncio.run(huawei_client._fetch_stats(config)) is None
        assert huawei_client._client is None
        assert huawei_client._plant_dn is None
    finally:
        huawei_client._get_client = _real_get_client
        huawei_client._resolve_plant_dn = real_resolve
        huawei_client._client = None
        huawei_client._plant_dn = None


def test_failures_accumulate_and_success_clears_them():
    _note_failure(1000.0)
    assert huawei_client._failure_streak == 1
    assert huawei_client._failure_until == 1060.0

    _note_failure(1060.0)
    assert huawei_client._failure_streak == 2
    assert huawei_client._failure_until == 1180.0

    _note_success()
    assert huawei_client._failure_streak == 0
    assert huawei_client._failure_until == 0.0


# --------------------------------------------------------------------------
# Staleness guard
# --------------------------------------------------------------------------

def test_a_fresh_point_is_not_stale():
    recent = datetime.now() - timedelta(minutes=5)
    assert _is_stale(recent.strftime("%Y-%m-%d %H:%M"), 900) is False


def test_an_old_point_is_stale():
    old = datetime.now() - timedelta(hours=3)
    assert _is_stale(old.strftime("%Y-%m-%d %H:%M"), 900) is True


def test_an_unprovable_timestamp_is_treated_as_fresh():
    """Staleness cannot be proven, so a good read is not thrown away."""
    assert _is_stale(None, 900) is False
    assert _is_stale("not a timestamp", 900) is False
