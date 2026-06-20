"""Electricity tariff model — turns energy-history buckets into cost & savings.

UI-free core for the Energy-tab cost breakdown (issue #46). Given the hourly
import/consumption/PV buckets from :mod:`src.energy_history`, it assigns each
hour to its time-of-use period, prices the grid energy, and values the
self-consumed PV (the savings).

**Tariff source.** Rates live in ``config/tariff.json`` (gitignored; a committed
``config/tariff.sample.json`` documents the shape). A missing or invalid file is
not an error — :func:`load_tariff` returns a flat 0.10 €/kWh fallback, the same
"graceful default" pattern as :mod:`src.display_names` / :mod:`src.webapp_config`.

**Prices are pre-tax.** Each period's ``price_eur_kwh`` is the energy commodity +
access tolls + system charges. The per-kWh electricity tax and VAT are applied
here, so the all-in marginal price of a kWh in period *P* is
``(price[P] + electricity_tax) * (1 + vat_pct/100)``. That all-in price is what a
grid kWh costs and — equivalently — what a self-consumed PV kWh saves.

**2.0TD calendar** (peninsular Spain, local time):

* **P1 punta** — Mon–Fri 10:00–14:00 and 18:00–22:00
* **P2 llano** — Mon–Fri 08:00–10:00, 14:00–18:00, 22:00–24:00
* **P3 valle** — Mon–Fri 00:00–08:00, plus all hours of Sat/Sun and holidays

This is a household-monitoring *estimate*, not a billing-grade meter read: PVPC
energy is genuinely hourly-indexed, so the per-period price is an average (see
``docs/tariff-model.md``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "tariff.json"

# Fallback when no tariff is configured — a single flat all-in rate, no taxes on
# top (the number is taken as-is) and no time-of-use split.
_FLAT_FALLBACK_EUR_KWH = 0.10

_DAYS_PER_YEAR = 365.0

# 2.0TD weekday hour → period. Index by local hour [0, 23]. Weekends/holidays are
# all-valle and handled before this table is consulted.
_TOU_2_0TD_BY_HOUR = (
    # 0    1    2    3    4    5    6    7   (00:00–08:00 valle)
    "P3", "P3", "P3", "P3", "P3", "P3", "P3", "P3",
    # 8    9   (08:00–10:00 llano)
    "P2", "P2",
    # 10   11   12   13  (10:00–14:00 punta)
    "P1", "P1", "P1", "P1",
    # 14   15   16   17  (14:00–18:00 llano)
    "P2", "P2", "P2", "P2",
    # 18   19   20   21  (18:00–22:00 punta)
    "P1", "P1", "P1", "P1",
    # 22   23  (22:00–24:00 llano)
    "P2", "P2",
)

# Human-readable hour ranges per 2.0TD period (for the cost-table time column).
_TOU_2_0TD_HOURS = {
    "P1": "10–14 · 18–22",
    "P2": "8–10 · 14–18 · 22–24",
    "P3": "0–8 · weekends",
}

# Display order earliest-to-latest by when each period first starts in the day
# (valle 00:00 → llano 08:00 → punta 10:00), so the table reads chronologically.
_TOU_2_0TD_ORDER = ("P3", "P2", "P1")


@dataclass(frozen=True)
class Period:
    """One time-of-use period (e.g. P1 peak) with its pre-tax energy price."""

    key: str
    label: str
    price_eur_kwh: float


@dataclass(frozen=True)
class Tariff:
    """A loaded tariff: periods, taxes, fixed charges and the TOU calendar."""

    currency: str
    name: str
    calendar: str  # "2.0TD" | "flat"
    vat_pct: float
    electricity_tax_eur_kwh: float
    export_eur_kwh: float
    periods: Dict[str, Period]
    period_order: List[str]
    holidays: frozenset
    fixed: Dict[str, float]
    configured: bool

    def marginal_all_in(self, period_key: str) -> float:
        """All-in €/kWh for ``period_key`` (price + electricity tax, + VAT)."""
        period = self.periods.get(period_key)
        if period is None:
            return 0.0
        pre_tax = period.price_eur_kwh + self.electricity_tax_eur_kwh
        return pre_tax * (1.0 + self.vat_pct / 100.0)

    def hours_label(self, period_key: str) -> str:
        """Human time-range hint for a period (2.0TD only; "" otherwise)."""
        if self.calendar != "2.0TD":
            return ""
        return _TOU_2_0TD_HOURS.get(period_key, "")

    def display_order(self) -> List[str]:
        """Period keys ordered earliest-to-latest for display."""
        if self.calendar == "2.0TD":
            return [k for k in _TOU_2_0TD_ORDER if k in self.periods]
        return list(self.period_order)

    def daily_fixed_eur(self) -> float:
        """Pre-tax standing charge per day (power terms + margin + meter rental)."""
        f = self.fixed
        power_kw = float(f.get("contracted_power_kw", 0.0) or 0.0)
        per_kw_year = (
            float(f.get("power_term_p1_eur_kw_year", 0.0) or 0.0)
            + float(f.get("power_term_p3_eur_kw_year", 0.0) or 0.0)
            + float(f.get("marketing_margin_eur_kw_year", 0.0) or 0.0)
        )
        meter_day = float(f.get("meter_rental_eur_day", 0.0) or 0.0)
        return power_kw * per_kw_year / _DAYS_PER_YEAR + meter_day


# --------------------------------------------------------------- loading
def _flat_tariff() -> Tariff:
    """The unconfigured fallback: one flat all-in rate, no TOU, no taxes added."""
    period = Period(key="FLAT", label="Flat", price_eur_kwh=_FLAT_FALLBACK_EUR_KWH)
    return Tariff(
        currency="EUR",
        name="Flat estimate",
        calendar="flat",
        vat_pct=0.0,
        electricity_tax_eur_kwh=0.0,
        export_eur_kwh=0.0,
        periods={"FLAT": period},
        period_order=["FLAT"],
        holidays=frozenset(),
        fixed={},
        configured=False,
    )


def load_tariff(path: Optional[Path] = None) -> Tariff:
    """Load the tariff from ``config/tariff.json``, or the flat fallback.

    A missing file, unreadable file, bad JSON, or a structurally invalid config
    all degrade to :func:`_flat_tariff` with a warning — the cost view stays up
    with a clearly-labelled estimate rather than 500-ing.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        return _flat_tariff()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); using flat estimate", target, exc)
        return _flat_tariff()
    if not isinstance(raw, dict):
        logger.warning("⚠️ %s is not a JSON object; using flat estimate", target)
        return _flat_tariff()

    try:
        periods_raw = raw.get("periods") or {}
        if not isinstance(periods_raw, dict) or not periods_raw:
            raise ValueError("no periods")
        periods: Dict[str, Period] = {}
        order: List[str] = []
        for key, spec in periods_raw.items():
            spec = spec or {}
            periods[str(key)] = Period(
                key=str(key),
                label=str(spec.get("label", key)),
                price_eur_kwh=float(spec.get("price_eur_kwh", 0.0)),
            )
            order.append(str(key))
        order.sort()  # P1, P2, P3 — stable, period-key order

        fixed_raw = raw.get("fixed") or {}
        fixed = {str(k): float(v) for k, v in fixed_raw.items()} if isinstance(fixed_raw, dict) else {}

        holidays_raw = raw.get("holidays") or []
        holidays = frozenset(str(d) for d in holidays_raw) if isinstance(holidays_raw, list) else frozenset()

        return Tariff(
            currency=str(raw.get("currency", "EUR")),
            name=str(raw.get("tariff_name", "Tariff")),
            calendar=str(raw.get("calendar", "2.0TD")),
            vat_pct=float(raw.get("vat_pct", 0.0)),
            electricity_tax_eur_kwh=float(raw.get("electricity_tax_eur_kwh", 0.0)),
            export_eur_kwh=float(raw.get("export_eur_kwh", 0.0)),
            periods=periods,
            period_order=order,
            holidays=holidays,
            fixed=fixed,
            configured=True,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("⚠️ %s is malformed (%s); using flat estimate", target, exc)
        return _flat_tariff()


# --------------------------------------------------------------- calendar
def period_for(dt: datetime, tariff: Tariff) -> str:
    """Return the period key for local datetime ``dt`` under ``tariff``.

    Non-TOU tariffs (``calendar != "2.0TD"``) map every hour to their single
    period. For 2.0TD, weekends and configured holidays are all-valle (P3);
    weekdays follow the punta/llano/valle hour table.
    """
    if tariff.calendar != "2.0TD":
        return tariff.period_order[0]
    if dt.weekday() >= 5 or dt.strftime("%Y-%m-%d") in tariff.holidays:
        return "P3"
    return _TOU_2_0TD_BY_HOUR[dt.hour]


# --------------------------------------------------------------- breakdown
def _empty_row(period: Period, rate_eur_kwh: float, hours: str) -> Dict[str, Any]:
    return {
        "key": period.key,
        "label": period.label,
        "hours": hours,
        "price_eur_kwh": round(period.price_eur_kwh, 6),
        "rate_eur_kwh": round(rate_eur_kwh, 4),
        "consumption_kwh": 0.0,
        "grid_kwh": 0.0,
        "solar_kwh": 0.0,
        "generation_kwh": 0.0,
        "export_kwh": 0.0,
        "grid_cost": 0.0,
        "savings": 0.0,
    }


def cost_breakdown(
    hourly_buckets: List[Dict[str, Any]],
    tariff: Tariff,
    days: float = 0.0,
) -> Dict[str, Any]:
    """Per-period + total cost/savings from hourly energy buckets.

    Each bucket carries Wh for ``pv_wh`` / ``house_wh`` / ``import_wh`` /
    ``export_wh`` (the :mod:`src.energy_history` shape) and ``hour_start`` (epoch
    seconds, local). Solar-covered consumption per hour is ``house − import``
    (≥ 0). Grid energy is priced at its period's all-in rate; self-consumed PV is
    valued at that same avoided rate (the savings). ``days`` prorates the fixed
    standing charge for the window.
    """
    rows = {
        key: _empty_row(tariff.periods[key], tariff.marginal_all_in(key), tariff.hours_label(key))
        for key in tariff.period_order
    }

    for b in hourly_buckets:
        dt = datetime.fromtimestamp(int(b["hour_start"]))
        pk = period_for(dt, tariff)
        row = rows.get(pk)
        if row is None:  # period not in config (shouldn't happen) — skip safely
            continue
        house_kwh = (b.get("house_wh") or 0.0) / 1000.0
        import_kwh = (b.get("import_wh") or 0.0) / 1000.0
        export_kwh = (b.get("export_wh") or 0.0) / 1000.0
        pv_kwh = 0.0 if b.get("pv_missing") else (b.get("pv_wh") or 0.0) / 1000.0
        solar_kwh = max(0.0, house_kwh - import_kwh)

        rate = tariff.marginal_all_in(pk)
        row["consumption_kwh"] += house_kwh
        row["grid_kwh"] += import_kwh
        row["solar_kwh"] += solar_kwh
        row["generation_kwh"] += pv_kwh
        row["export_kwh"] += export_kwh
        row["grid_cost"] += import_kwh * rate
        row["savings"] += solar_kwh * rate

    period_rows = [rows[key] for key in tariff.display_order()]

    totals = {
        "consumption_kwh": 0.0, "grid_kwh": 0.0, "solar_kwh": 0.0,
        "generation_kwh": 0.0, "export_kwh": 0.0, "grid_cost": 0.0, "savings": 0.0,
    }
    for row in period_rows:
        for k in totals:
            totals[k] += row[k]

    export_credit = totals["export_kwh"] * tariff.export_eur_kwh
    fixed_cost = tariff.daily_fixed_eur() * max(0.0, days) * (1.0 + tariff.vat_pct / 100.0)
    # What the grid bill would have been buying every consumed kWh from the grid.
    cost_without_solar = totals["grid_cost"] + totals["savings"]
    estimated_bill = totals["grid_cost"] + fixed_cost - export_credit

    summary = {
        "fixed_cost": fixed_cost,
        "export_credit": export_credit,
        "cost_without_solar": cost_without_solar,
        "estimated_bill": estimated_bill,
        "days": round(days, 2),
    }

    return {
        "currency": tariff.currency,
        "tariff_name": tariff.name,
        "calendar": tariff.calendar,
        "configured": tariff.configured,
        "periods": [_round_row(r) for r in period_rows],
        "totals": _round_money(totals),
        "summary": _round_money(summary),
    }


def _round_row(row: Dict[str, Any]) -> Dict[str, Any]:
    for k in ("consumption_kwh", "grid_kwh", "solar_kwh", "generation_kwh", "export_kwh"):
        row[k] = round(row[k], 2)
    for k in ("grid_cost", "savings"):
        row[k] = round(row[k], 2)
    return row


def _round_money(d: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in d.items():
        if isinstance(v, float):
            d[k] = round(v, 2)
    return d
