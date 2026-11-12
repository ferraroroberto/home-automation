"""Tesla history store: drives and charging sessions built from samples."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from src.devices import tesla_history as th
from src.devices.tesla_client import TeslaSample

T0 = 1_760_000_000  # a fixed instant; buckets below are checked relative to it


def _s(minute: int, **kw) -> TeslaSample:
    base = dict(
        ts=T0 + minute * 60, battery_pct=80.0, range_km=380.0, charging_state="Disconnected",
        charger_power_kw=0.0, charge_energy_added_kwh=0.0, odometer_km=10_000.0,
        shift_state=None, speed_kmh=None, at_home=True,
    )
    base.update(kw)
    return TeslaSample(**base)


# Parked at home -> 25 km drive (80% -> 70%) -> parked away -> plugged in at
# home later, charging 70% -> 85% with energy accumulating to 9.1 kWh, closed by
# a Complete read.
SEQUENCE = [
    _s(0),
    _s(2, shift_state="D", speed_kmh=40.0, battery_pct=79.0, odometer_km=10_001.0),
    _s(10, shift_state="D", speed_kmh=90.0, battery_pct=74.0, odometer_km=10_012.0),
    _s(20, shift_state="D", speed_kmh=60.0, battery_pct=71.0, odometer_km=10_022.0),
    _s(24, shift_state="P", speed_kmh=None, battery_pct=70.0, odometer_km=10_025.0, at_home=False),
    _s(60, battery_pct=70.0, odometer_km=10_025.0, charging_state="Charging",
       charger_power_kw=7.2, charge_energy_added_kwh=0.5),
    _s(70, battery_pct=73.0, odometer_km=10_025.0, charging_state="Charging",
       charger_power_kw=7.4, charge_energy_added_kwh=1.8),
    _s(140, battery_pct=84.0, odometer_km=10_025.0, charging_state="Charging",
       charger_power_kw=7.1, charge_energy_added_kwh=8.9),
    _s(148, battery_pct=85.0, odometer_km=10_025.0, charging_state="Complete",
       charger_power_kw=0.0, charge_energy_added_kwh=9.1),
]


def test_build_sessions_from_fixed_sequence() -> None:
    drives, charges = th.build_sessions(SEQUENCE, usable_kwh=60.0)

    assert len(drives) == 1
    drive = drives[0]
    assert drive.km == pytest.approx(25.0)
    assert drive.start_ts == T0 + 2 * 60  # the first driving read, not the parked anchor
    assert drive.end_ts == T0 + 24 * 60
    assert drive.energy_kwh == pytest.approx(6.0)  # 10 % of 60 kWh

    assert len(charges) == 1
    session = charges[0]
    assert session.kwh_added == pytest.approx(9.1)  # the closing Complete read's final figure
    assert session.peak_kw == pytest.approx(7.4)
    assert (session.start_pct, session.end_pct) == (70.0, 85.0)
    assert session.location == "home"
    assert session.end_ts == T0 + 148 * 60


def test_drive_missed_by_the_poll_still_counts() -> None:
    """Two parked reads with the odometer moved between them are a drive."""
    parked = [_s(0), _s(30, odometer_km=10_004.5, battery_pct=78.0)]
    drives, _ = th.build_sessions(parked, usable_kwh=60.0)
    assert [(d.km, d.start_ts) for d in drives] == [(4.5, T0)]


def test_open_sessions_are_not_emitted() -> None:
    """A drive or charge still in progress has no closing read yet."""
    drives, _ = th.build_sessions(SEQUENCE[:3], usable_kwh=60.0)
    _, charges = th.build_sessions(SEQUENCE[5:7], usable_kwh=60.0)
    assert drives == [] and charges == []


def test_charging_run_closed_by_a_long_silence() -> None:
    run = [SEQUENCE[5], SEQUENCE[6], replace(SEQUENCE[8], ts=T0 + 70 * 60 + th._SESSION_GAP_S + 60)]
    _, charges = th.build_sessions(run, usable_kwh=60.0)
    assert charges[0].end_ts == SEQUENCE[6].ts
    assert charges[0].kwh_added == pytest.approx(1.8)


def test_store_round_trip_is_idempotent(tmp_path) -> None:
    db = tmp_path / "tesla.sqlite3"
    th.init_db(db)
    for sample in SEQUENCE:
        th.record_sample(sample, db)
    config = th.TeslaHistoryConfig(usable_kwh=60.0)
    th.refresh_sessions(config, db)
    th.refresh_sessions(config, db)

    sessions = th.recent_sessions(path=db)
    assert len(sessions["drives"]) == 1 and len(sessions["charging"]) == 1
    assert sessions["drives"][0]["km"] == pytest.approx(25.0)
    assert sessions["charging"][0]["kwh_added"] == pytest.approx(9.1)

    th.record_status("asleep", ts=T0 + 9000, path=db)
    latest = th.latest(db)
    assert latest["state"] == "asleep" and latest["checked_at"] == T0 + 9000
    assert latest["sample"]["battery_pct"] == 85.0
    assert "latitude" not in latest["sample"] and "longitude" not in latest["sample"]

    for period in ("week", "month"):
        buckets = th.history(period, db)
        assert sum(b["km"] for b in buckets) == pytest.approx(25.0)
        assert sum(b["charged_kwh"] for b in buckets) == pytest.approx(9.1)
        assert sum(b["charged_home_kwh"] for b in buckets) == pytest.approx(9.1)
    week = th.history("week", db)[0]
    assert week["kwh_per_100km"] == pytest.approx(24.0)


def test_prune_drops_old_samples_but_keeps_sessions(tmp_path) -> None:
    db = tmp_path / "tesla.sqlite3"
    th.init_db(db)
    old = int(time.time()) - 100 * 86400
    for sample in SEQUENCE:
        th.record_sample(replace(sample, ts=sample.ts - T0 + old), db)
    th.refresh_sessions(th.TeslaHistoryConfig(), db)
    assert th.prune(th.TeslaHistoryConfig(raw_retention_days=90), db) == len(SEQUENCE)
    assert th.latest(db)["sample"] is None
    assert len(th.recent_sessions(path=db)["drives"]) == 1


def test_history_rejects_unknown_period(tmp_path) -> None:
    db = tmp_path / "tesla.sqlite3"
    th.init_db(db)
    with pytest.raises(ValueError):
        th.history("year", db)


def test_energy_counter_restart_splits_back_to_back_sessions() -> None:
    first = [SEQUENCE[5], SEQUENCE[6]]
    second = [replace(SEQUENCE[5], ts=SEQUENCE[6].ts + 120, charge_energy_added_kwh=0.2), SEQUENCE[8]]
    second[1] = replace(second[1], ts=second[0].ts + 600)
    _, charges = th.build_sessions(first + second, usable_kwh=60.0)
    assert [c.kwh_added for c in charges] == [pytest.approx(1.8), pytest.approx(9.1)]


def _store_refreshing_after_each(samples, db) -> None:
    """The live sampler's cadence: a refresh after every stored sample."""
    th.init_db(db)
    config = th.TeslaHistoryConfig(usable_kwh=60.0)
    for sample in samples:
        th.record_sample(sample, db)
        th.refresh_sessions(config, db)


def _charging_rows(db):
    rows = th.recent_sessions(path=db)["charging"]
    return sorted((c["start_ts"], c["end_ts"], c["kwh_added"]) for c in rows)


def test_incremental_refresh_matches_a_single_build(tmp_path) -> None:
    """Structural check: however a session closes, refreshing per sample stores what one pass builds."""
    restarted = replace(SEQUENCE[5], ts=SEQUENCE[6].ts + 120, charge_energy_added_kwh=0.2)
    after_gap = replace(SEQUENCE[8], ts=restarted.ts + th._SESSION_GAP_S + 60,
                        charging_state="Disconnected", charge_energy_added_kwh=0.0)
    parked_later = replace(after_gap, ts=after_gap.ts + 300)
    streams = {
        "normal close": SEQUENCE,
        "gap close": [SEQUENCE[5], SEQUENCE[6], after_gap, parked_later],
        "counter restart then gap": [SEQUENCE[5], SEQUENCE[6], restarted, after_gap, parked_later],
    }
    for name, stream in streams.items():
        db = tmp_path / f"{name.replace(' ', '_')}.sqlite3"
        _store_refreshing_after_each(stream, db)
        drives, charges = th.build_sessions(stream, usable_kwh=60.0)
        assert _charging_rows(db) == sorted(
            (c.start_ts, c.end_ts, pytest.approx(c.kwh_added)) for c in charges
        ), name
        assert len(th.recent_sessions(path=db)["drives"]) == len(drives), name


def test_gap_closed_session_is_stored_once(tmp_path) -> None:
    """A run closed at its last charging read must not be re-read as a new one-sample session."""
    db = tmp_path / "tesla.sqlite3"
    after_gap = replace(SEQUENCE[8], ts=SEQUENCE[6].ts + th._SESSION_GAP_S + 60,
                        charging_state="Disconnected", charge_energy_added_kwh=0.0)
    parked_later = replace(after_gap, ts=after_gap.ts + 300)
    _store_refreshing_after_each([SEQUENCE[5], SEQUENCE[6], after_gap, parked_later], db)

    assert _charging_rows(db) == [(SEQUENCE[5].ts, SEQUENCE[6].ts, pytest.approx(1.8))]
    assert sum(b["charged_kwh"] for b in th.history("month", db)) == pytest.approx(1.8)


def test_counter_restart_closed_session_is_stored_once(tmp_path) -> None:
    """Back-to-back sessions split by the energy counter restarting are each stored exactly once."""
    db = tmp_path / "tesla.sqlite3"
    restarted = replace(SEQUENCE[5], ts=SEQUENCE[6].ts + 120, charge_energy_added_kwh=0.2)
    done = replace(SEQUENCE[8], ts=restarted.ts + 600)
    _store_refreshing_after_each([SEQUENCE[5], SEQUENCE[6], restarted, done], db)

    assert _charging_rows(db) == [
        (SEQUENCE[5].ts, SEQUENCE[6].ts, pytest.approx(1.8)),
        (restarted.ts, done.ts, pytest.approx(9.1)),
    ]
    assert sum(b["charged_kwh"] for b in th.history("month", db)) == pytest.approx(10.9)
