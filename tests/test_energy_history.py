"""Unit tests for :mod:`src.energy_history` — the SQLite energy store.

Runs entirely against a ``tmp_path`` SQLite DB with explicit ``ts``/``now``, so
there is no real clock, cloud, or shared-DB dependence. Energy is integrated by
the rectangular rule from raw samples; the assertions hand-compute the Wh.
"""

from __future__ import annotations

import time
from pathlib import Path

from src import energy_history as H
from src.huawei_client import EnergyState


def _state(pv=None, house=None, imp=None, exp=None) -> EnergyState:
    return EnergyState(
        grid_import_w=imp,
        grid_export_w=exp,
        pv_power_w=pv,
        house_consumption_w=house,
        pv_surplus_w=None,
        meter_reachable=True,
        inverter_reachable=pv is not None,
    )


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    H.init_db(db)  # second call must not raise
    assert db.exists()


def test_record_and_recent_samples_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    now = int(time.time())
    H.record_sample(_state(pv=2400.0, house=1200.0, imp=0.0, exp=1200.0), ts=now, path=db)

    samples = H.recent_samples(minutes=60, path=db)
    assert len(samples) == 1
    s = samples[0]
    assert s["ts"] == now
    assert s["pv_power_w"] == 2400.0
    assert s["house_consumption_w"] == 1200.0
    assert s["inverter_reachable"] is True


def test_recent_samples_preserves_none_for_asleep_pv(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    now = int(time.time())
    # Asleep inverter: pv_power_w must stay None, never coerced to 0.
    H.record_sample(_state(pv=None, house=300.0, imp=300.0, exp=0.0), ts=now, path=db)
    samples = H.recent_samples(minutes=60, path=db)
    assert samples[0]["pv_power_w"] is None
    assert samples[0]["inverter_reachable"] is False


def test_aggregate_hourly_integrates_energy(tmp_path: Path) -> None:
    """Three samples 60s apart integrate to a hand-computed Wh bucket."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200          # top of an hour (1_699_999_200 % 3600 == 0)
    assert base % H._HOUR == 0
    for off in (0, 60, 120):
        H.record_sample(
            _state(pv=2400.0, house=1200.0, imp=0.0, exp=1200.0),
            ts=base + off,
            path=db,
        )
    now = base + 200  # still inside the same hour

    buckets = H.aggregate("hourly", count=1, now=now, path=db)
    assert len(buckets) == 1
    b = buckets[0]
    # Rectangular rule over two 60s intervals (last sample has no following dt):
    #   power * dt / 3600, summed.
    assert b["house_wh"] == 40.0   # 1200 * 60/3600 * 2
    assert b["pv_wh"] == 80.0      # 2400 * 60/3600 * 2
    assert b["export_wh"] == 40.0  # 1200 * 60/3600 * 2
    assert b["import_wh"] == 0.0
    assert b["pv_n"] == 3
    assert b["pv_missing"] is False


def test_aggregate_unknown_period_raises(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    try:
        H.aggregate("decadely", path=db)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown period")


def test_framed_buckets_day_is_24_padded_slots(tmp_path: Path) -> None:
    """The ``day`` frame always returns 24 hourly slots, padding empty hours."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    now = 1_700_000_000
    out = H.framed_buckets("day", now=now, path=db)
    assert len(out) == 24
    # No samples recorded → every slot is an empty 0-Wh bucket.
    assert all(b["house_wh"] == 0.0 and b["pv_wh"] == 0.0 for b in out)


def test_only_the_hour_containing_now_is_flagged_partial(tmp_path: Path) -> None:
    """#557: the still-filling slot is flagged so the UI can mark it in progress."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    assert base % H._HOUR == 0
    # One sample in the previous hour, one in the hour that contains `now`.
    H.record_sample(_state(pv=2400.0, house=1200.0), ts=base - H._HOUR, path=db)
    H.record_sample(_state(pv=2400.0, house=1200.0), ts=base + 60, path=db)
    now = base + 600

    buckets = H.aggregate("hourly", count=2, now=now, path=db)
    assert [b["partial"] for b in buckets] == [False, True]


def test_a_settled_hour_is_never_flagged_partial(tmp_path: Path) -> None:
    """A short *past* hour is genuinely that low — it must not be marked."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    H.record_sample(_state(pv=2400.0, house=1200.0), ts=base + 60, path=db)
    # `now` is well into a later hour, so the sampled hour is fully settled. The
    # window has to reach back far enough to include it — hourly aggregation
    # returns only hours that actually hold data.
    buckets = H.aggregate("hourly", count=4, now=base + 3 * H._HOUR, path=db)
    assert [int(b["key"]) for b in buckets] == [base]
    assert buckets[0]["partial"] is False


def test_framed_buckets_day_flags_the_current_hour(tmp_path: Path) -> None:
    """Exactly one of the 24 day slots is in progress, and it is the right one."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    now = 1_700_000_000
    out = H.framed_buckets("day", now=now, path=db)
    flagged = [i for i, b in enumerate(out) if b["partial"]]
    assert len(flagged) == 1
    hour_start = int(out[flagged[0]]["key"])
    assert hour_start <= now < hour_start + H._HOUR


def test_hourly_day_flags_no_partial_for_a_past_day(tmp_path: Path) -> None:
    """Yesterday is entirely settled — nothing on it is still filling."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    out = H.hourly_day(-1, now=1_700_000_000, path=db)
    assert len(out) == 24
    assert not any(b["partial"] for b in out)


# ----------------------------------------------- partial PV coverage (#579)
def _fill_hour(db: Path, base: int, pv_from: int, pv_to: int, step: int = 60) -> None:
    """Sample a whole hour at ``step``, with PV present only in ``[pv_from, pv_to)``."""
    for off in range(0, H._HOUR, step):
        pv = 2400.0 if pv_from <= off < pv_to else None
        H.record_sample(_state(pv=pv, house=1200.0), ts=base + off, path=db)


def test_a_fully_covered_hour_is_not_flagged_as_a_gap(tmp_path: Path) -> None:
    """The normal path: an hour of samples is a measurement, however low."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    _fill_hour(db, base, 0, H._HOUR)

    b = H.aggregate("hourly", count=3, now=base + 2 * H._HOUR, path=db)[0]
    # The hour's last sample opens no further interval, so coverage tops out
    # just under 1.0 — which is exactly why the trust threshold is not 1.0.
    assert b["pv_coverage"] > H.MIN_TRUSTED_COVERAGE
    assert b["pv_gap"] is False
    assert b["pv_missing"] is False


def test_a_half_covered_hour_is_flagged_as_a_gap(tmp_path: Path) -> None:
    """#579: 30 of 60 minutes of PV data is under-measured, not a low hour."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    _fill_hour(db, base, 0, 30 * 60)

    b = H.aggregate("hourly", count=3, now=base + 2 * H._HOUR, path=db)[0]
    assert b["pv_missing"] is False          # there *was* PV data — not asleep
    assert b["pv_gap"] is True               # …but not enough of it to trust
    assert 0.45 < b["pv_coverage"] < 0.55
    assert b["pv_seconds"] == 30 * 60.0


def test_an_hour_with_no_pv_at_all_is_missing_not_a_gap(tmp_path: Path) -> None:
    """Asleep and gapped are different claims; a night hour must not be an outage."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    _fill_hour(db, base, 0, 0)

    b = H.aggregate("hourly", count=3, now=base + 2 * H._HOUR, path=db)[0]
    assert b["pv_missing"] is True
    assert b["pv_gap"] is False
    assert b["pv_coverage"] == 0.0


def test_the_in_progress_hour_is_measured_against_elapsed_time(tmp_path: Path) -> None:
    """Otherwise every current hour would look like an outage for 59 minutes."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    for off in range(0, 600, 60):
        H.record_sample(_state(pv=2400.0, house=1200.0), ts=base + off, path=db)

    b = H.aggregate("hourly", count=1, now=base + 600, path=db)[0]
    assert b["partial"] is True
    assert b["pv_gap"] is False              # 9 of 10 elapsed minutes covered
    assert b["pv_coverage"] > H.MIN_TRUSTED_COVERAGE


def test_the_top_of_an_hour_is_not_an_outage(tmp_path: Path) -> None:
    """Found replaying real history: 0 s elapsed made every ratio 0/0.

    Unguarded, the in-progress hour was declared a feed outage on the stroke of
    every hour and cleared itself minutes later.
    """
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    H.record_sample(_state(pv=2400.0, house=1200.0), ts=base, path=db)

    b = H.aggregate("hourly", count=1, now=base + 5, path=db)[0]
    assert b["partial"] is True
    assert b["pv_gap"] is False


def test_an_hour_that_generated_nothing_is_never_a_gap(tmp_path: Path) -> None:
    """Found replaying real history: the inverter flaps 0 W ↔ asleep overnight.

    2026-07-30 read 45% and 10% PV coverage at 00:00 and 01:00 — which would
    have reported 1.4 h of "solar feed offline" in the middle of the night. With
    no measured generation there is nothing to be short of, and no projection
    could recover any: scaling 0 up still gives 0.
    """
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    for off in range(0, H._HOUR, 60):
        pv = 0.0 if (off // 60) % 4 == 0 else None   # a quarter of the samples
        H.record_sample(_state(pv=pv, house=300.0), ts=base + off, path=db)

    b = H.aggregate("hourly", count=3, now=base + 2 * H._HOUR, path=db)[0]
    assert b["pv_wh"] == 0.0
    assert b["pv_coverage"] < H.MIN_TRUSTED_COVERAGE   # genuinely sparse…
    assert b["pv_gap"] is False                         # …but not an outage


def test_legacy_rollups_fall_back_to_the_sample_count_ratio(tmp_path: Path) -> None:
    """Rows stored before #579 have no pv_seconds; pv_n / n stands in for it."""
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    base = 1_699_999_200
    with H._connect(db) as conn:
        conn.execute(
            "INSERT INTO rollup_hourly (hour_start, n, pv_n, pv_wh, house_wh,"
            " import_wh, export_wh, pv_avg_w, house_avg_w, pv_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (base, 59, 15, 400.0, 1200.0, 0.0, 0.0, 427.0, 1200.0),
        )
        conn.commit()

    b = H.aggregate("hourly", count=3, now=base + 2 * H._HOUR, path=db)[0]
    assert b["pv_gap"] is True               # 15/59 ≈ 0.25 — the reported hour
    assert 0.2 < b["pv_coverage"] < 0.3


def test_init_db_migrates_a_pre_579_rollup_table(tmp_path: Path) -> None:
    """An existing DB gains the column in place, keeping its rows."""
    db = tmp_path / "h.sqlite3"
    with H._connect(db) as conn:
        conn.executescript(
            "CREATE TABLE rollup_hourly (hour_start INTEGER PRIMARY KEY,"
            " n INTEGER NOT NULL, pv_n INTEGER NOT NULL, pv_wh REAL,"
            " house_wh REAL, import_wh REAL, export_wh REAL, pv_avg_w REAL,"
            " house_avg_w REAL);"
            "INSERT INTO rollup_hourly VALUES (1699999200, 60, 60, 500.0,"
            " 100.0, 0.0, 0.0, 500.0, 100.0);"
        )
        conn.commit()

    H.init_db(db)

    with H._connect(db) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(rollup_hourly)")}
        rows = conn.execute("SELECT * FROM rollup_hourly").fetchall()
    assert "pv_seconds" in columns
    assert len(rows) == 1 and rows[0]["pv_wh"] == 500.0


def test_hourly_range_unknown_period_raises(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    try:
        H.hourly_range("fortnight", path=db)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown period")
