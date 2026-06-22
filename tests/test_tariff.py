"""Unit tests for :mod:`src.tariff` — the tiered electricity cost/savings math.

Pure logic: no network, no clock dependence. The 2.0TD calendar is tested via
explicit naive datetimes (timezone-independent); the cost arithmetic is tested
against the flat fallback tariff so the per-period rate is a single known number
and the totals are hand-computable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src import tariff as T


# --------------------------------------------------------------- load_tariff
def test_load_tariff_missing_file_is_flat_fallback(tmp_path: Path) -> None:
    t = T.load_tariff(tmp_path / "does-not-exist.json")
    assert t.configured is False
    assert t.calendar == "flat"
    assert t.marginal_all_in("FLAT") == T._FLAT_FALLBACK_EUR_KWH


def test_load_tariff_malformed_json_is_flat_fallback(tmp_path: Path) -> None:
    bad = tmp_path / "tariff.json"
    bad.write_text("{not valid json", encoding="utf-8")
    t = T.load_tariff(bad)
    assert t.configured is False
    assert t.calendar == "flat"


def test_load_tariff_round_trip_from_sample_shape(tmp_path: Path) -> None:
    cfg = {
        "currency": "EUR",
        "tariff_name": "Test 2.0TD",
        "calendar": "2.0TD",
        "holidays": ["2026-01-06"],
        "vat_pct": 10,
        "electricity_tax_eur_kwh": 0.001,
        "periods": {
            "P1": {"label": "Peak", "price_eur_kwh": 0.20},
            "P2": {"label": "Standard", "price_eur_kwh": 0.13},
            "P3": {"label": "Off-peak", "price_eur_kwh": 0.11},
        },
        "export_eur_kwh": 0.05,
    }
    path = tmp_path / "tariff.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    t = T.load_tariff(path)
    assert t.configured is True
    assert t.calendar == "2.0TD"
    assert t.currency == "EUR"
    assert set(t.periods) == {"P1", "P2", "P3"}
    assert "2026-01-06" in t.holidays
    # marginal_all_in = (price + electricity_tax) * (1 + vat/100).
    assert t.marginal_all_in("P1") == (0.20 + 0.001) * 1.10


# --------------------------------------------------------------- period_for
def test_period_for_weekday_hours() -> None:
    t = _two_0td()
    # Monday 2026-06-22. Punta 10–14 & 18–22, llano 08–10/14–18/22–24, valle 0–8.
    assert T.period_for(datetime(2026, 6, 22, 3), t) == "P3"   # 03:00 valle
    assert T.period_for(datetime(2026, 6, 22, 9), t) == "P2"   # 09:00 llano
    assert T.period_for(datetime(2026, 6, 22, 11), t) == "P1"  # 11:00 punta
    assert T.period_for(datetime(2026, 6, 22, 15), t) == "P2"  # 15:00 llano
    assert T.period_for(datetime(2026, 6, 22, 20), t) == "P1"  # 20:00 punta
    assert T.period_for(datetime(2026, 6, 22, 23), t) == "P2"  # 23:00 llano


def test_period_for_weekend_is_all_valle() -> None:
    t = _two_0td()
    # 2026-06-20 is a Saturday, 2026-06-21 a Sunday — every hour is P3.
    assert T.period_for(datetime(2026, 6, 20, 11), t) == "P3"
    assert T.period_for(datetime(2026, 6, 21, 20), t) == "P3"


def test_period_for_holiday_is_all_valle() -> None:
    t = _two_0td(holidays=["2026-06-22"])
    # A configured holiday falling on a weekday is still all-valle.
    assert T.period_for(datetime(2026, 6, 22, 11), t) == "P3"


def test_period_for_flat_tariff_single_period() -> None:
    flat = T.load_tariff(Path("missing"))
    assert T.period_for(datetime(2026, 6, 22, 11), flat) == "FLAT"


# --------------------------------------------------------------- cost_breakdown
def test_cost_breakdown_flat_hand_computed() -> None:
    """Flat tariff (rate 0.10, no taxes/TOU) makes the math fully deterministic."""
    flat = T.load_tariff(Path("missing"))  # FLAT @ 0.10 €/kWh
    buckets = [
        {"hour_start": 1_700_000_000, "house_wh": 2000, "import_wh": 1500,
         "export_wh": 0, "pv_wh": 500},
        {"hour_start": 1_700_003_600, "house_wh": 1000, "import_wh": 400,
         "export_wh": 100, "pv_wh": 800},
    ]
    out = T.cost_breakdown(buckets, flat, days=0.0)

    totals = out["totals"]
    assert totals["consumption_kwh"] == 3.0   # 2.0 + 1.0
    assert totals["grid_kwh"] == 1.9          # 1.5 + 0.4
    assert totals["solar_kwh"] == 1.1         # (2.0-1.5) + (1.0-0.4)
    assert totals["generation_kwh"] == 1.3    # 0.5 + 0.8
    assert totals["export_kwh"] == 0.1
    assert totals["grid_cost"] == 0.19        # 1.9 * 0.10
    assert totals["savings"] == 0.11          # 1.1 * 0.10

    summary = out["summary"]
    assert summary["cost_without_solar"] == 0.30  # grid_cost + savings
    assert summary["fixed_cost"] == 0.0           # flat fallback has no fixed terms
    assert summary["export_credit"] == 0.0        # flat export_eur_kwh = 0
    assert summary["estimated_bill"] == 0.19      # grid_cost + fixed - credit


def test_cost_breakdown_solar_never_negative() -> None:
    """import > house (grid feeding a non-PV moment) must not yield negative solar."""
    flat = T.load_tariff(Path("missing"))
    buckets = [{"hour_start": 1_700_000_000, "house_wh": 500, "import_wh": 900,
                "export_wh": 0, "pv_wh": 0}]
    out = T.cost_breakdown(buckets, flat)
    assert out["totals"]["solar_kwh"] == 0.0


def test_cost_breakdown_pv_missing_excluded_from_generation() -> None:
    """A bucket flagged ``pv_missing`` contributes 0 generation, not its pv_wh."""
    flat = T.load_tariff(Path("missing"))
    buckets = [{"hour_start": 1_700_000_000, "house_wh": 1000, "import_wh": 1000,
                "export_wh": 0, "pv_wh": 9999, "pv_missing": True}]
    out = T.cost_breakdown(buckets, flat)
    assert out["totals"]["generation_kwh"] == 0.0


# --------------------------------------------------------------- helpers
def _two_0td(holidays=None) -> T.Tariff:
    """Build a 2.0TD tariff in-memory (avoids depending on a config file)."""
    periods = {
        "P1": T.Period("P1", "Peak", 0.20),
        "P2": T.Period("P2", "Standard", 0.13),
        "P3": T.Period("P3", "Off-peak", 0.11),
    }
    return T.Tariff(
        currency="EUR",
        name="Test 2.0TD",
        calendar="2.0TD",
        vat_pct=10.0,
        electricity_tax_eur_kwh=0.001,
        export_eur_kwh=0.0,
        periods=periods,
        period_order=["P1", "P2", "P3"],
        holidays=frozenset(holidays or []),
        fixed={},
        configured=True,
    )
