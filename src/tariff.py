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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import read_json, save_json

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


@dataclass(frozen=True, order=True)
class ExportRate:
    """One surplus-compensation rate effective from a local calendar date."""

    effective_from: date
    export_eur_kwh: float
    hourly_eur_kwh: tuple[Optional[float], ...] = ()


@dataclass(frozen=True)
class Tariff:
    """A loaded tariff: periods, taxes, fixed charges and the TOU calendar."""

    currency: str
    name: str
    calendar: str  # "2.0TD" | "flat"
    vat_pct: float
    electricity_tax_eur_kwh: float
    export_rates: List[ExportRate]
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
        export_rates=[],
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

        export_rates_raw = raw.get("export_rates")
        if export_rates_raw is None:
            legacy_rate = float(raw.get("export_eur_kwh", 0.0))
            export_rates = [ExportRate(date.min, legacy_rate)]
        else:
            if not isinstance(export_rates_raw, list):
                raise ValueError("export_rates must be a list")
            export_rates = _parse_export_rates(export_rates_raw)

        return Tariff(
            currency=str(raw.get("currency", "EUR")),
            name=str(raw.get("tariff_name", "Tariff")),
            calendar=str(raw.get("calendar", "2.0TD")),
            vat_pct=float(raw.get("vat_pct", 0.0)),
            electricity_tax_eur_kwh=float(raw.get("electricity_tax_eur_kwh", 0.0)),
            export_rates=export_rates,
            periods=periods,
            period_order=order,
            holidays=holidays,
            fixed=fixed,
            configured=True,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("⚠️ %s is malformed (%s); using flat estimate", target, exc)
        return _flat_tariff()


def _parse_export_rates(raw_rates: List[Any]) -> List[ExportRate]:
    """Validate and order the persisted export-rate history."""
    rates: List[ExportRate] = []
    seen = set()
    for index, item in enumerate(raw_rates):
        if not isinstance(item, dict):
            raise ValueError(f"export_rates[{index}] must be an object")
        try:
            effective_from = date.fromisoformat(str(item["effective_from"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"export_rates[{index}].effective_from must be YYYY-MM-DD"
            ) from exc
        try:
            value = float(item["export_eur_kwh"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"export_rates[{index}].export_eur_kwh must be a number"
            ) from exc
        if not 0.0 <= value <= 10.0:
            raise ValueError(
                f"export_rates[{index}].export_eur_kwh must be between 0 and 10"
            )
        if effective_from in seen:
            raise ValueError(f"export_rates has duplicate date {effective_from.isoformat()}")
        seen.add(effective_from)
        hourly_raw = item.get("hourly_eur_kwh")
        hourly: tuple[Optional[float], ...] = ()
        if hourly_raw is not None:
            if not isinstance(hourly_raw, list) or len(hourly_raw) != 24:
                raise ValueError(f"export_rates[{index}].hourly_eur_kwh must contain 24 values")
            parsed_hourly: List[Optional[float]] = []
            for hour, hourly_value in enumerate(hourly_raw):
                if hourly_value is None:
                    parsed_hourly.append(None)
                    continue
                try:
                    number = float(hourly_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"export_rates[{index}].hourly_eur_kwh[{hour}] must be a number or null"
                    ) from exc
                if not 0.0 <= number <= 10.0:
                    raise ValueError(
                        f"export_rates[{index}].hourly_eur_kwh[{hour}] must be between 0 and 10"
                    )
                parsed_hourly.append(number)
            hourly = tuple(parsed_hourly)
        rates.append(ExportRate(effective_from, value, hourly))
    return sorted(rates)


def rate_for(dt: datetime, tariff: Tariff) -> float:
    """Return the latest export rate effective on or before local ``dt``."""
    for rate in reversed(tariff.export_rates):
        if rate.effective_from <= dt.date():
            if len(rate.hourly_eur_kwh) == 24 and rate.hourly_eur_kwh[dt.hour] is not None:
                return float(rate.hourly_eur_kwh[dt.hour])
            return rate.export_eur_kwh
    return 0.0


def export_rates_payload(tariff: Tariff) -> List[Dict[str, Any]]:
    """Return the public JSON shape for a tariff's dated export rates."""
    payload = []
    for rate in tariff.export_rates:
        entry: Dict[str, Any] = {
            "effective_from": rate.effective_from.isoformat(),
            "export_eur_kwh": rate.export_eur_kwh,
        }
        if rate.hourly_eur_kwh:
            entry["hourly_eur_kwh"] = list(rate.hourly_eur_kwh)
        payload.append(entry)
    return payload


def save_export_rate(
    effective_from: str,
    export_eur_kwh: float,
    path: Optional[Path] = None,
    hourly_eur_kwh: Optional[List[Optional[float]]] = None,
    replace_effective_from: Optional[str] = None,
) -> Tariff:
    """Atomically upsert one dated export rate while preserving the tariff file."""
    target = Path(path) if path is not None else DEFAULT_PATH
    raw = read_json(target, None)
    if not isinstance(raw, dict):
        raise ValueError("tariff is not configured")
    try:
        rate_date = date.fromisoformat(effective_from)
    except (TypeError, ValueError) as exc:
        raise ValueError("effective_from must be YYYY-MM-DD") from exc
    try:
        value = float(export_eur_kwh)
    except (TypeError, ValueError) as exc:
        raise ValueError("export_eur_kwh must be a number") from exc
    if not 0.0 <= value <= 10.0:
        raise ValueError("export_eur_kwh must be between 0 and 10")

    existing = raw.get("export_rates")
    if existing is None:
        rates = [ExportRate(date.min, float(raw.get("export_eur_kwh", 0.0)))]
    elif isinstance(existing, list):
        rates = _parse_export_rates(existing)
    else:
        raise ValueError("export_rates must be a list")
    by_date = {rate.effective_from: rate for rate in rates}
    hourly = _parse_export_rates([{
        "effective_from": effective_from,
        "export_eur_kwh": value,
        "hourly_eur_kwh": hourly_eur_kwh,
    }])[0].hourly_eur_kwh
    if replace_effective_from:
        try:
            by_date.pop(date.fromisoformat(replace_effective_from), None)
        except ValueError as exc:
            raise ValueError("replace_effective_from must be YYYY-MM-DD") from exc
    by_date[rate_date] = ExportRate(rate_date, value, hourly)
    raw.pop("export_eur_kwh", None)
    raw["export_rates"] = export_rates_payload(Tariff(
        "EUR", "", "flat", 0, 0, sorted(by_date.values()), {}, [], frozenset(), {}, True
    ))
    save_json(target, raw)
    logger.info("💶 Saved export rate %.6f EUR/kWh from %s", value, effective_from)
    return load_tariff(target)


def delete_export_rate(effective_from: str, path: Optional[Path] = None) -> Tariff:
    """Delete one dated rate while preserving every other tariff field."""
    target = Path(path) if path is not None else DEFAULT_PATH
    raw = read_json(target, None)
    if not isinstance(raw, dict):
        raise ValueError("tariff is not configured")
    try:
        target_date = date.fromisoformat(effective_from)
    except (TypeError, ValueError) as exc:
        raise ValueError("effective_from must be YYYY-MM-DD") from exc
    rates_raw = raw.get("export_rates")
    if rates_raw is None:
        raise ValueError("legacy rate cannot be deleted; add a dated rate first")
    rates = _parse_export_rates(rates_raw)
    remaining = [rate for rate in rates if rate.effective_from != target_date]
    if len(remaining) == len(rates):
        raise ValueError(f"no export rate exists for {effective_from}")
    raw["export_rates"] = export_rates_payload(Tariff(
        "EUR", "", "flat", 0, 0, remaining, {}, [], frozenset(), {}, True
    ))
    save_json(target, raw)
    logger.info("🗑️ Deleted export rate effective %s", effective_from)
    return load_tariff(target)


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
        "export_credit": 0.0,
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
    money_series: List[Dict[str, Any]] = []

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
        bucket_credit = export_kwh * rate_for(dt, tariff)
        row["export_credit"] += bucket_credit
        money_series.append({
            "hour_start": int(b["hour_start"]),
            "label": dt.strftime("%d %b %H:%M"),
            "grid_cost": round(import_kwh * rate, 6),
            "savings": round(solar_kwh * rate, 6),
            "export_credit": round(bucket_credit, 6),
        })
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

    export_credit = sum(row["export_credit"] for row in period_rows)
    fixed_cost = tariff.daily_fixed_eur() * max(0.0, days) * (1.0 + tariff.vat_pct / 100.0)
    # What the grid bill would have been buying every consumed kWh from the grid.
    cost_without_solar = totals["grid_cost"] + totals["savings"]
    estimated_bill = totals["grid_cost"] + fixed_cost - export_credit

    summary = {
        "fixed_cost": fixed_cost,
        "export_credit": export_credit,
        "total_solar_benefit": totals["savings"] + export_credit,
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
        "money_series": money_series,
    }


def group_money_series(
    hourly_points: List[Dict[str, Any]], range_: str
) -> List[Dict[str, Any]]:
    """Group hourly money points to the Energy tab's selected chart range."""
    formats = {
        "day": ("%Y-%m-%d-%H", "%H:00"),
        "week": ("%Y-%m-%d", "%a %d"),
        "month": ("%Y-%m-%d", "%d %b"),
        "year": ("%Y-%m", "%b"),
        "total": ("total", "Total"),
    }
    if range_ not in formats:
        raise ValueError(f"unsupported range: {range_}")
    key_format, label_format = formats[range_]
    grouped: Dict[str, Dict[str, Any]] = {}
    for point in hourly_points:
        dt = datetime.fromtimestamp(int(point["hour_start"]))
        key = "total" if range_ == "total" else dt.strftime(key_format)
        bucket = grouped.setdefault(key, {
            "label": "Total" if range_ == "total" else dt.strftime(label_format),
            "grid_cost": 0.0,
            "savings": 0.0,
            "export_credit": 0.0,
        })
        for field in ("grid_cost", "savings", "export_credit"):
            bucket[field] += float(point.get(field) or 0.0)
    return [
        {
            **bucket,
            "grid_cost": round(bucket["grid_cost"], 6),
            "savings": round(bucket["savings"], 6),
            "export_credit": round(bucket["export_credit"], 6),
        }
        for bucket in grouped.values()
    ]


def _round_row(row: Dict[str, Any]) -> Dict[str, Any]:
    for k in ("consumption_kwh", "grid_kwh", "solar_kwh", "generation_kwh", "export_kwh"):
        row[k] = round(row[k], 2)
    for k in ("grid_cost", "savings", "export_credit"):
        row[k] = round(row[k], 2)
    return row


def _round_money(d: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in d.items():
        if isinstance(v, float):
            d[k] = round(v, 2)
    return d
