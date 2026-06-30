"""Unit tests for :mod:`src.telemetry` — the unified telemetry SQLite store.

Runs entirely against a ``tmp_path`` SQLite DB with explicit ``ts``/``now``, so
there is no real clock, cloud, or shared-DB dependence (mirrors the pattern in
``test_energy_history.py``).
"""

from __future__ import annotations

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


def test_reading_asleep_stays_null_never_zero(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    T.init_db(db)
    now = int(time.time())
    # Asleep inverter: a missing numeric metric must persist as NULL, not 0.
    T.record_readings(
        [Reading("energy", "site", "pv_power_w", value_num=None, unit="W", quality="unreachable")],
        ts=now,
        path=db,
    )
    rows = T.read_readings(metric="pv_power_w", path=db)
    assert rows[0]["value_num"] is None
    assert rows[0]["quality"] == "unreachable"


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
