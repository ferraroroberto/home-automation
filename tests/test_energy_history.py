"""Unit tests for :mod:`src.energy_history` — the SQLite energy store.

Runs entirely against a ``tmp_path`` SQLite DB with explicit ``ts``/``now``, so
there is no real clock, cloud, or shared-DB dependence. Energy is integrated by
the rectangular rule from raw samples; the assertions hand-compute the Wh.
"""

from __future__ import annotations

import time
from pathlib import Path

from src import energy_history as H
from src.sma_client import EnergyState


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


def test_hourly_range_unknown_period_raises(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    H.init_db(db)
    try:
        H.hourly_range("fortnight", path=db)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown period")
