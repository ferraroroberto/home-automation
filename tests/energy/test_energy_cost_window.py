"""Unit tests for :func:`app.webapp.routers.energy._window_days` (issue #429).

Pure logic: the fixed-cost proration must be capped at the actual span of
retained history, not the nominal calendar length of the requested range —
otherwise a young history gets charged a full year of fixed cost against a
few weeks of real consumption.
"""

from __future__ import annotations

import time

import pytest

from app.webapp.routers import energy as E


def _bucket_days_ago(days: float) -> dict:
    return {"hour_start": int(time.time() - days * 86_400.0)}


def test_window_days_no_buckets_is_zero() -> None:
    assert E._window_days("month", []) == 0.0
    assert E._window_days("total", []) == 0.0


def test_window_days_capped_by_actual_span_when_history_is_young() -> None:
    # Only ~23 days of real history — less than "month" (30) and "year" (365).
    buckets = [_bucket_days_ago(23.0)]
    assert E._window_days("month", buckets) == pytest.approx(23.0, abs=0.01)
    assert E._window_days("year", buckets) == pytest.approx(23.0, abs=0.01)
    assert E._window_days("total", buckets) == pytest.approx(23.0, abs=0.01)


def test_window_days_uses_nominal_once_history_exceeds_it() -> None:
    # Plenty of history — nominal windows apply as before.
    buckets = [_bucket_days_ago(400.0)]
    assert E._window_days("day", buckets) == 1.0
    assert E._window_days("week", buckets) == 7.0
    assert E._window_days("month", buckets) == 30.0
    assert E._window_days("year", buckets) == 365.0
    # "total" has no nominal cap — it always reflects the real span.
    assert E._window_days("total", buckets) == pytest.approx(400.0, abs=0.01)


def test_window_days_month_year_total_converge_for_young_history() -> None:
    """The exact bug from #429: month/year/total must agree while history is young."""
    buckets = [_bucket_days_ago(23.0)]
    month = E._window_days("month", buckets)
    year = E._window_days("year", buckets)
    total = E._window_days("total", buckets)
    assert month == pytest.approx(year, abs=0.001)
    assert year == pytest.approx(total, abs=0.001)
