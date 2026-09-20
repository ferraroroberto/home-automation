"""Unit tests for :mod:`src.telemetry` — the unified telemetry SQLite store.

Runs entirely against a ``tmp_path`` SQLite DB with explicit ``ts``/``now``, so
there is no real clock, cloud, or shared-DB dependence (mirrors the pattern in
``test_energy_history.py``).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src import telemetry as T
from src.telemetry import Reading, TelemetryConfig


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    T.init_db(db)  # second call must not raise
    assert db.exists()


def test_record_and_read_readings_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = int(time.time())
    written = T.record_readings(
        [
            Reading("hvac", "unit-1", "room_temperature", value_num=21.5, unit="degC", quality="ok"),
            Reading("hvac", "unit-1", "operation_mode", value_txt="heat", quality="ok"),
        ],
        ts=now,
        path=db,
    )
    assert written == 2

    rows = T.read_readings(domain="hvac", path=db)
    assert len(rows) == 2
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["room_temperature"]["value_num"] == 21.5
    assert by_metric["room_temperature"]["unit"] == "degC"
    assert by_metric["operation_mode"]["value_txt"] == "heat"
    assert all(r["ts"] == now for r in rows)


def test_record_readings_empty_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    assert T.record_readings([], path=db) == 0
    assert T.read_readings(path=db) == []


def test_empty_reading_skipped_but_zero_kept(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = int(time.time())
    written = T.record_readings(
        [
            # No value at all (offline / non-metering) -> dropped, not a NULL row.
            Reading("plug", "d1", "power_w", value_num=None, unit="W", quality="unreachable"),
            # A real zero is a reading, not "missing" -> kept.
            Reading("plug", "d2", "power_w", value_num=0.0, unit="W"),
            # A categorical value with no number -> kept.
            Reading("hvac", "u1", "operation_mode", value_txt="heat"),
        ],
        ts=now,
        path=db,
    )
    assert written == 2
    rows = T.read_readings(path=db)
    metrics = {(r["entity_id"], r["metric"]) for r in rows}
    assert ("d1", "power_w") not in metrics  # empty reading dropped
    zero = [r for r in rows if r["entity_id"] == "d2"][0]
    assert zero["value_num"] == 0.0  # zero preserved, never dropped


def test_read_events_type_is_substring_match(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    T.record_event("plug", "plug_on", ts=1, path=db)
    T.record_event("plug", "plug_off", ts=2, path=db)
    T.record_event("alarm", "arm", ts=3, path=db)
    assert {e["event_type"] for e in T.read_events(event_type="plug", path=db)} == {"plug_on", "plug_off"}
    assert [e["event_type"] for e in T.read_events(event_type="on", path=db)] == ["plug_on"]


def test_read_readings_metric_is_substring_match(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    T.record_readings(
        [Reading("plug", "d", "power_w", value_num=5.0), Reading("plug", "d", "voltage_v", value_num=1.0)],
        ts=1,
        path=db,
    )
    # "w" matches power_w, not voltage_v — the fix for the exact-match filter bug.
    assert [r["metric"] for r in T.read_readings(metric="w", path=db)] == ["power_w"]


def test_attrs_json_sidecar_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    T.record_readings(
        [Reading("plug", "dev-9", "power_w", value_num=42.0, attrs={"dps": 19, "scale": 0.1})],
        path=db,
    )
    rows = T.read_readings(entity_id="dev-9", path=db)
    assert rows[0]["attrs"] == {"dps": 19, "scale": 0.1}


def test_record_and_read_event_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = int(time.time())
    T.record_event(
        "security",
        "arm",
        entity_id="partition-1",
        source="manual",
        outcome="ok",
        severity="info",
        payload={"mode": "away"},
        ts=now,
        path=db,
    )
    events = T.read_events(domain="security", path=db)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "arm"
    assert e["source"] == "manual"
    assert e["payload"] == {"mode": "away"}
    assert e["ts"] == now


def test_read_events_filters_and_orders_newest_first(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    base = 1_700_000_000
    T.record_event("security", "arm", source="manual", ts=base, path=db)
    T.record_event("power", "power_lost", ts=base + 10, path=db)
    T.record_event("security", "disarm", source="schedule", ts=base + 20, path=db)

    # Filter by domain.
    sec = T.read_events(domain="security", path=db)
    assert [e["event_type"] for e in sec] == ["disarm", "arm"]  # newest first

    # Filter by event_type.
    lost = T.read_events(event_type="power_lost", path=db)
    assert len(lost) == 1 and lost[0]["domain"] == "power"

    # since/until window.
    windowed = T.read_events(since=base + 5, until=base + 20, path=db)
    assert [e["event_type"] for e in windowed] == ["power_lost"]

    # limit caps the result.
    assert len(T.read_events(limit=1, path=db)) == 1


def test_compact_and_prune_drops_aged_rows_keeps_fresh(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    cfg = TelemetryConfig(readings_retention_days=7, events_retention_days=400)
    now = 2_000_000_000
    day = 24 * 3600

    # A fresh + an aged reading; a fresh + an aged event.
    T.record_readings([Reading("hvac", "u1", "room_temperature", value_num=20.0)], ts=now - 1 * day, path=db)
    T.record_readings([Reading("hvac", "u1", "room_temperature", value_num=19.0)], ts=now - 30 * day, path=db)
    T.record_event("power", "power_lost", ts=now - 1 * day, path=db)
    T.record_event("power", "power_lost", ts=now - 500 * day, path=db)

    T.compact_and_prune(cfg, now=now, path=db)

    readings = T.read_readings(path=db)
    assert len(readings) == 1 and readings[0]["value_num"] == 20.0  # 30-day-old reading pruned
    events = T.read_events(path=db)
    assert len(events) == 1  # 500-day-old event pruned, 1-day kept


# ------------------------------------------------------------------ rollups
# Hourly rollups (#739). These are guards, not regression proofs: the feature
# is new, so "fails before the fix" would only mean "the function did not exist
# yet". They pin the behaviours the issue's acceptance criteria name —
# aggregation, asleep-is-not-zero through the rollup, coverage of a gappy hour,
# the categorical rule, and aggregate-before-prune ordering.
_H = 3600


def _cfg(**kw) -> TelemetryConfig:
    """A config with the live defaults unless a test overrides one."""
    base = dict(readings_retention_days=7, events_retention_days=400,
                rollup_retention_days=400, sample_interval_s=300)
    base.update(kw)
    return TelemetryConfig(**base)


def _hour_of(ts: int) -> int:
    return ts - (ts % _H)


def _rollup_rows(db: Path) -> list:
    """Read rollup_hourly back raw — the store is write-side only (#739)."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM rollup_hourly")]
    finally:
        conn.close()


def _rollups(db: Path) -> dict:
    """rollup_hourly keyed by (entity_id, metric) — for single-hour tests."""
    return {(r["entity_id"], r["metric"]): r for r in _rollup_rows(db)}


def _rolled_hours(db: Path) -> set:
    return {int(r["hour_start"]) for r in _rollup_rows(db)}


def _sample_hour(db: Path, hour: int, values: list, cadence: int = 300) -> None:
    """Write one numeric reading per cadence step from the hour's start."""
    for i, value in enumerate(values):
        T.record_readings(
            [Reading("hvac", "u1", "room_temperature", value_num=value, unit="degC")],
            ts=hour + i * cadence,
            path=db,
        )


def test_rollup_aggregates_numeric_avg_min_max(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    hour = _hour_of(2_000_000_000) - _H  # one completed hour back
    _sample_hour(db, hour, [19.0, 20.0, 21.0, 22.0, 23.0, 24.0,
                            19.0, 20.0, 21.0, 22.0, 23.0, 24.0])

    T.compact_and_prune(_cfg(), now=hour + _H + 60, path=db)

    roll = _rollups(db)[("u1", "room_temperature")]
    assert roll["n"] == 12 and roll["num_n"] == 12
    assert roll["avg_num"] == 21.5
    assert roll["min_num"] == 19.0 and roll["max_num"] == 24.0
    assert roll["unit"] == "degC"
    assert roll["hour_start"] == hour


def test_rollup_keeps_asleep_as_null_never_zero(tmp_path: Path) -> None:
    """A fully-covered hour whose numbers were all absent must not average to 0."""
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    hour = _hour_of(2_000_000_000) - _H
    # Categorical-only readings: the sampler ran all hour, the number never came.
    for i in range(12):
        T.record_readings(
            [Reading("hvac", "u1", "room_temperature", value_num=None, value_txt="off")],
            ts=hour + i * 300,
            path=db,
        )

    T.compact_and_prune(_cfg(), now=hour + _H + 60, path=db)

    roll = _rollups(db)[("u1", "room_temperature")]
    assert roll["num_n"] == 0
    assert roll["avg_num"] is None and roll["min_num"] is None and roll["max_num"] is None
    # ...and the hour is reported as fully covered, so this reads as "asleep",
    # not as "the sampler was down".
    assert T.rollup_coverage(roll) == {"coverage": 1.0, "gap": False}


def test_rollup_coverage_flags_a_gappy_hour_but_not_a_healthy_one(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    healthy = _hour_of(2_000_000_000) - 2 * _H
    gappy = healthy + _H

    _sample_hour(db, healthy, [20.0] * 12)       # full hour at cadence
    _sample_hour(db, gappy, [20.0, 20.5, 21.0])  # sampler died 15 min in

    T.compact_and_prune(_cfg(), now=gappy + _H + 60, path=db)

    rows = {int(r["hour_start"]): r for r in _rollup_rows(db)}

    assert T.rollup_coverage(rows[healthy]) == {"coverage": 1.0, "gap": False}
    # Two 300 s gaps between the three readings, then the last one stands for
    # the rest of the hour only up to the 600 s cap: 300 + 300 + 600 = 1200 s.
    # The 40 uncovered minutes after the sampler died are exactly the point.
    assert rows[gappy]["covered_s"] == 1200.0
    assert T.rollup_coverage(rows[gappy]) == {"coverage": 0.333, "gap": True}
    # The average is still honest about the minutes that arrived...
    assert rows[gappy]["avg_num"] == 20.5
    # ...and only coverage separates the two hours' credibility.
    assert rows[gappy]["n"] == 3 and rows[healthy]["n"] == 12


def test_rollup_coverage_threshold_tracks_a_slower_cadence(tmp_path: Path) -> None:
    """Halving the sample rate must not turn every hour into a phantom outage."""
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    hour = _hour_of(2_000_000_000) - _H
    _sample_hour(db, hour, [20.0] * 6, cadence=600)  # 10-minute cadence

    T.compact_and_prune(_cfg(sample_interval_s=600), now=hour + _H + 60, path=db)

    roll = _rollups(db)[("u1", "room_temperature")]
    assert T.rollup_coverage(roll) == {"coverage": 1.0, "gap": False}


def test_rollup_categorical_keeps_last_value_and_counts_changes(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    hour = _hour_of(2_000_000_000) - _H
    modes = ["heat"] * 9 + ["cool", "cool", "heat"]
    for i, mode in enumerate(modes):
        T.record_readings(
            [Reading("hvac", "u1", "operation_mode", value_txt=mode)],
            ts=hour + i * 300,
            path=db,
        )

    T.compact_and_prune(_cfg(), now=hour + _H + 60, path=db)

    roll = _rollups(db)[("u1", "operation_mode")]
    assert roll["txt_last"] == "heat"   # the hour's final observed state
    assert roll["txt_changes"] == 2     # heat->cool, cool->heat
    assert roll["avg_num"] is None      # never averaged
    assert roll["num_n"] == 0


def test_rollup_lands_before_the_prune_so_history_survives(tmp_path: Path) -> None:
    """The acceptance criterion: aged-out raw rows leave a rollup behind."""
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = 2_000_000_000
    aged = _hour_of(now - 30 * 24 * _H)   # far past the 7-day raw window
    fresh = _hour_of(now) - _H

    _sample_hour(db, aged, [18.0] * 12)
    _sample_hour(db, fresh, [21.0] * 12)

    T.compact_and_prune(_cfg(), now=now, path=db)

    # Raw: only the fresh hour survives the 7-day prune.
    assert {_hour_of(r["ts"]) for r in T.read_readings(path=db)} == {fresh}
    # Rollups: both hours are there - the 30-day-old hour kept its history.
    assert _rolled_hours(db) == {aged, fresh}
    assert _rollups(db)[("u1", "room_temperature")]["avg_num"] == 21.0


def test_rollup_skips_the_in_progress_hour_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = 2_000_000_000
    current = _hour_of(now)
    done = current - _H

    _sample_hour(db, done, [20.0] * 12)
    _sample_hour(db, current, [30.0, 30.0])  # still filling

    T.compact_and_prune(_cfg(), now=now, path=db)
    first = _rollup_rows(db)
    assert _rolled_hours(db) == {done}

    T.compact_and_prune(_cfg(), now=now, path=db)  # re-run must not duplicate
    assert _rollup_rows(db) == first


def test_rollup_retention_prunes_rollups_on_their_own_window(tmp_path: Path) -> None:
    """Rollups outlive raw, but are themselves bounded by their own knob.

    Uses a deliberately tiny 2-day rollup window so the hour-by-hour walk stays
    cheap; the default's value is pinned separately below.
    """
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    cfg = _cfg(readings_retention_days=1, rollup_retention_days=2)
    now = 2_000_000_000
    ancient = _hour_of(now) - 3 * 24 * _H  # past the 2-day rollup window
    recent = _hour_of(now) - 2 * _H        # inside it

    _sample_hour(db, ancient, [10.0] * 12)
    _sample_hour(db, recent, [11.0] * 12)

    T.compact_and_prune(cfg, now=now, path=db)

    # Both hours were aggregated before the prunes ran, then the rollup window
    # dropped only the one past it.
    assert _rolled_hours(db) == {recent}
    # Raw kept only its own 1-day window: the ancient hour is gone from raw
    # (and from the rollups too, being past both windows), the recent one is
    # still raw *and* rolled up.
    assert {_hour_of(r["ts"]) for r in T.read_readings(path=db)} == {recent}


def test_rollup_retention_defaults_to_the_events_window(tmp_path: Path) -> None:
    """The .env knob's default (#739): rollups are kept as long as events."""
    cfg = T.TelemetryConfig()
    assert cfg.rollup_retention_days == 400 == cfg.events_retention_days
    assert cfg.rollup_retention_seconds == 400 * 24 * _H
    # Coverage is judged against the sampler's cadence, not a fixed constant.
    assert cfg.max_gap_seconds == 2 * cfg.sample_interval_s == 600


def test_rollup_separates_each_series(tmp_path: Path) -> None:
    """One row per (hour, domain, entity, metric) - series never blend."""
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    hour = _hour_of(2_000_000_000) - _H
    for i in range(12):
        T.record_readings(
            [
                Reading("hvac", "u1", "room_temperature", value_num=20.0),
                Reading("hvac", "u2", "room_temperature", value_num=30.0),
                Reading("plug", "p1", "power_w", value_num=100.0),
            ],
            ts=hour + i * 300,
            path=db,
        )

    T.compact_and_prune(_cfg(), now=hour + _H + 60, path=db)

    rolls = _rollups(db)
    assert len(rolls) == 3
    assert rolls[("u1", "room_temperature")]["avg_num"] == 20.0
    assert rolls[("u2", "room_temperature")]["avg_num"] == 30.0
    assert rolls[("p1", "power_w")]["domain"] == "plug"
