"""Internet-sample series behind the tile's sparklines (#840).

Pure SQLite over a tmp path — no network, no speed test. The speed figures here
are fixture numbers handed straight to the recorder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import network_history as nh

NOW = 1_700_000_000


def _ping(ms: float, online: bool = True) -> dict:
    return {
        "online": online,
        "external_ms": ms,
        "gateway_ms": 1.0,
        "packet_loss_pct": 0.0,
        "download_mbps": None,
        "upload_mbps": None,
    }


def test_missing_store_reads_as_empty(tmp_path: Path) -> None:
    history = nh.internet_history(now=NOW, path=tmp_path / "none.sqlite3")
    assert history["latency"] == []
    assert history["download"] == []
    assert history["upload"] == []


def test_ping_samples_are_throttled_to_one_a_minute(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    assert nh.record_internet_sample(_ping(10), NOW, db) is True
    assert nh.record_internet_sample(_ping(11), NOW + 15, db) is False
    assert nh.record_internet_sample(_ping(12), NOW + 59, db) is False
    assert nh.record_internet_sample(_ping(13), NOW + 60, db) is True
    latency = nh.internet_history(now=NOW + 120, path=db)["latency"]
    assert [v for _ts, v in latency] == [10, 13]


def test_a_speed_result_is_kept_inside_the_throttle_window(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    nh.record_internet_sample(_ping(10), NOW, db)
    speed = {**_ping(10), "download_mbps": 300.0, "upload_mbps": 40.0}
    assert nh.record_internet_sample(speed, NOW + 5, db) is True
    history = nh.internet_history(now=NOW + 10, path=db)
    assert history["download"] == [[NOW + 5, 300.0]]
    assert history["upload"] == [[NOW + 5, 40.0]]


def test_offline_samples_leave_a_gap_not_a_zero(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    nh.record_internet_sample(_ping(10), NOW, db)
    nh.record_internet_sample(
        {"online": False, "external_ms": None, "gateway_ms": None, "packet_loss_pct": 100.0},
        NOW + 60,
        db,
    )
    nh.record_internet_sample(_ping(12), NOW + 120, db)
    latency = nh.internet_history(now=NOW + 180, path=db)["latency"]
    assert [v for _ts, v in latency] == [10, 12]


def test_latency_window_and_retention(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    old = NOW - 40 * 86400
    nh.record_internet_sample(_ping(99), old, db)
    nh.record_internet_sample(_ping(10), NOW - 25 * 3600, db)  # outside the 24 h window
    nh.record_internet_sample(_ping(20), NOW - 3600, db)
    latency = nh.internet_history(now=NOW, path=db)["latency"]
    assert [v for _ts, v in latency] == [20]
    # The 40-day-old row was pruned by the later write, not merely hidden.
    with nh._connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM internet_samples").fetchone()["n"] == 2


def test_latency_is_bucket_averaged_to_a_bounded_point_count(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    for i in range(300):
        nh.record_internet_sample(_ping(10 + (i % 5)), NOW - 23 * 3600 + i * 60, db)
    latency = nh.internet_history(now=NOW, path=db)["latency"]
    assert 0 < len(latency) <= 96
    assert latency == sorted(latency)
    assert all(10 <= v <= 14 for _ts, v in latency)


def test_speed_series_keeps_only_the_latest_results(tmp_path: Path) -> None:
    db = tmp_path / "h.sqlite3"
    for i in range(40):
        nh.record_internet_sample(
            {**_ping(10), "download_mbps": float(i), "upload_mbps": float(i) / 10},
            NOW - 20 * 86400 + i * 3600,
            db,
        )
    history = nh.internet_history(now=NOW, path=db)
    assert len(history["download"]) == 30
    assert history["download"][-1][1] == 39.0
    assert [p[0] for p in history["download"]] == sorted(p[0] for p in history["download"])


@pytest.mark.parametrize("hours", [1, 24])
def test_window_is_echoed_for_the_client(tmp_path: Path, hours: int) -> None:
    history = nh.internet_history(latency_hours=hours, now=NOW, path=tmp_path / "h.sqlite3")
    assert history["latency_window_h"] == hours
