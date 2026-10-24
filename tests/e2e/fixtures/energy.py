"""Energy endpoint stub: live flow, history, cost, export rates, PV system, boost, forecast."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._routes import json_body


def _default_snapshot() -> Dict:
    return {
        "grid_import_w": 0.0, "grid_export_w": 1200.0,
        "pv_power_w": 2500.0, "house_consumption_w": 1300.0,
        "pv_surplus_w": 1200.0, "grid_import_kwh": None, "grid_export_kwh": None,
        "meter_reachable": True, "inverter_reachable": True, "meter_serial": None,
    }


def _default_history() -> List[Dict]:
    return [
        {"ts": 1700000000, "pv_power_w": 2400.0, "house_consumption_w": 1200.0,
         "grid_import_w": 0.0, "grid_export_w": 1200.0, "pv_surplus_w": 1200.0,
         "inverter_reachable": True, "meter_reachable": True},
        {"ts": 1700000060, "pv_power_w": 2500.0, "house_consumption_w": 1300.0,
         "grid_import_w": 0.0, "grid_export_w": 1200.0, "pv_surplus_w": 1200.0,
         "inverter_reachable": True, "meter_reachable": True},
    ]


def _default_buckets() -> List[Dict]:
    return [
        {"key": "2026-06-19T10", "label": "10:00", "pv_wh": 1800.0,
         "house_wh": 1100.0, "import_wh": 0.0, "export_wh": 700.0,
         "pv_n": 60, "pv_missing": False},
        {"key": "2026-06-19T11", "label": "11:00", "pv_wh": 2100.0,
         "house_wh": 1250.0, "import_wh": 50.0, "export_wh": 900.0,
         "pv_n": 60, "pv_missing": False},
    ]


def _default_today() -> Dict:
    return {
        "key": "2026-06-19", "label": "Fri 19", "pv_wh": 9000.0,
        "house_wh": 6000.0, "import_wh": 500.0, "export_wh": 3500.0,
        "pv_n": 300, "pv_missing": False,
    }


def _default_cost() -> Dict:
    return {
        "currency": "EUR", "tariff_name": "Test 2.0TD", "calendar": "2.0TD",
        "configured": True, "range": "day",
        "periods": [
            {"key": "P3", "label": "Off-peak", "hours": "0–8 · weekends",
             "price_eur_kwh": 0.11, "rate_eur_kwh": 0.121,
             "consumption_kwh": 2.0, "grid_kwh": 1.5, "solar_kwh": 0.5,
             "generation_kwh": 0.5, "export_kwh": 0.0, "grid_cost": 0.18, "savings": 0.06},
            {"key": "P2", "label": "Standard", "hours": "8–10 · 14–18 · 22–24",
             "price_eur_kwh": 0.13, "rate_eur_kwh": 0.143,
             "consumption_kwh": 1.0, "grid_kwh": 0.4, "solar_kwh": 0.6,
             "generation_kwh": 0.8, "export_kwh": 0.1, "grid_cost": 0.06, "savings": 0.09},
            {"key": "P1", "label": "Peak", "hours": "10–14 · 18–22",
             "price_eur_kwh": 0.2, "rate_eur_kwh": 0.22,
             "consumption_kwh": 1.5, "grid_kwh": 0.5, "solar_kwh": 1.0,
             "generation_kwh": 1.2, "export_kwh": 0.2, "grid_cost": 0.11, "savings": 0.22},
        ],
        "totals": {"consumption_kwh": 4.5, "grid_kwh": 2.4, "solar_kwh": 2.1,
                   "generation_kwh": 2.5, "export_kwh": 0.3, "grid_cost": 0.35, "savings": 0.37},
        "summary": {"fixed_cost": 0.58, "export_credit": 0.0, "total_solar_benefit": 0.37,
                    "cost_without_solar": 0.72, "estimated_bill": 0.93, "days": 1.0},
        "money_series": [
            {"label": "10:00", "hour_start": 1_718_788_400,
             "grid_cost": 0.08, "savings": 0.04, "export_credit": 0.02},
        ],
    }


def _json_route(page: Page, pattern: str, body: Dict) -> None:
    page.route(pattern, lambda r: r.fulfill(
        status=200, content_type="application/json", body=json_body(body)))


def _route_live_series(
    page: Page, snap: Dict, hist: List[Dict], aggs: List[Dict],
    today_bucket: Dict, today_gap_hours: float,
) -> None:
    _json_route(page, "**/api/energy", snap)
    # `gap_hours` rides beside the bucket, not inside it (#579), and drives
    # a different card than the forecast overlay does — so it is its own
    # knob rather than a field of `today`.
    _json_route(page, "**/api/energy/today",
                {"bucket": today_bucket, "gap_hours": today_gap_hours})
    _json_route(page, "**/api/energy/history*", {"minutes": 60, "samples": hist})
    _json_route(page, "**/api/energy/aggregate*", {"range": "day", "buckets": aggs})


def _route_export_rates(page: Page) -> None:
    export_rates = {
        "configured": True, "currency": "EUR", "current_export_eur_kwh": 0.05,
        "rates": [{"effective_from": "2026-01-01", "export_eur_kwh": 0.05}],
    }

    def handle_export_rates(route: Route) -> None:
        method = route.request.method.upper()
        if method == "PUT":
            sent = route.request.post_data_json or {}
            original = sent.pop("replace_effective_from", None)
            export_rates["rates"] = [
                rate for rate in export_rates["rates"]
                if rate["effective_from"] != original
            ]
            export_rates["rates"].append({
                key: value for key, value in sent.items() if value is not None
            })
            export_rates["rates"].sort(key=lambda rate: rate["effective_from"])
            export_rates["current_export_eur_kwh"] = sent["export_eur_kwh"]
        elif method == "DELETE":
            from urllib.parse import parse_qs, urlparse
            effective = parse_qs(urlparse(route.request.url).query)["effective_from"][0]
            export_rates["rates"] = [
                rate for rate in export_rates["rates"]
                if rate["effective_from"] != effective
            ]
        route.fulfill(status=200, content_type="application/json", body=json_body(export_rates))

    page.route("**/api/energy/export-rates*", handle_export_rates)


def _route_pv_system(page: Page, pv_arrays: Optional[List[Dict]]) -> Dict:
    """Install the stateful PV-system route; return its rows for the forecast routes."""
    pv = {
        "arrays": list(pv_arrays) if pv_arrays is not None else [
            {"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0},
        ],
        "performance_ratio": 0.8,
    }

    def _pv_body() -> Dict:
        return {
            "configured": bool(pv["arrays"]),
            "arrays": pv["arrays"],
            "performance_ratio": pv["performance_ratio"],
            "total_kwp": round(sum(a["kwp"] for a in pv["arrays"]), 3),
        }

    def handle_pv_system(route: Route) -> None:
        if route.request.method.upper() == "PUT":
            sent = route.request.post_data_json or {}
            pv["arrays"] = sent.get("arrays") or []
            if sent.get("performance_ratio") is not None:
                pv["performance_ratio"] = sent["performance_ratio"]
        route.fulfill(
            status=200, content_type="application/json", body=json_body(_pv_body()))

    page.route("**/api/energy/pv-system", handle_pv_system)
    return pv


def _route_boost_coordinator(page: Page, boost_coord: Optional[Dict]) -> None:
    # The fleet solar-boost sequencing knobs (issue #562). Path is
    # /api/hvac/... because it is HVAC config, but it is stubbed here because
    # its card lives on the Energy tab — so every Energy-tab test that
    # already calls this fixture keeps its network stubbed.
    boost = {
        "settle_interval_s": 300, "admission_margin_w": 0.0,
        "hard_deficit_w": 1000.0,
    }
    if boost_coord:
        boost.update(boost_coord)

    def _boost_body() -> Dict:
        return dict(boost, min_settle_interval_s=300)

    def handle_boost_coord(route: Route) -> None:
        if route.request.method.upper() == "PUT":
            sent = route.request.post_data_json or {}
            # Mirrors the server: an out-of-range value is a 400 naming the
            # field, never a silent clamp.
            settle = sent.get("settle_interval_s")
            if settle is not None and int(settle) < 300:
                route.fulfill(
                    status=400, content_type="application/json",
                    body=json_body({"detail": "settle_interval_s must be at least 300 seconds"}))
                return
            for key, value in sent.items():
                if value is not None:
                    boost[key] = value
        route.fulfill(
            status=200, content_type="application/json", body=json_body(_boost_body()))

    page.route("**/api/hvac/boost-coordinator", handle_boost_coord)


def _pv_system_block(pv: Dict) -> Dict:
    return {
        "arrays": pv["arrays"],
        "total_kwp": round(sum(a["kwp"] for a in pv["arrays"]), 3),
        "performance_ratio": pv["performance_ratio"],
    }


def _route_forecast(page: Page, pv: Dict, forecast: Optional[Dict]) -> None:
    def handle_forecast(route: Route) -> None:
        body = {
            "available": True,
            "day": "today",
            "expected": [{"hour": h, "wh": 0.0 if h < 8 else 900.0} for h in range(24)],
            "expected_total_kwh": 12.3,
            "actual": None,
            "actual_gap_hours": None,
            "system": _pv_system_block(pv),
        }
        # `system` stays derived from the (stateful) PV rows even when a
        # test overrides the curve, so the save-reflects-in-the-card tests
        # keep working alongside an overridden actual overlay.
        if forecast:
            body.update(forecast)
        route.fulfill(status=200, content_type="application/json", body=json_body(body))

    page.route("**/api/energy/forecast*", handle_forecast)


def _route_sun_overlay(page: Page, pv: Dict, sun_overlay: Optional[Dict]) -> None:
    # Read-only sun-position diagnostic (#590). Stubbed here for the same
    # reason the forecast is: its card lives on this tab and an unstubbed
    # call would have the disposable instance reach Open-Meteo for real.
    # Default is an empty-but-available overlay, so a test that never opens
    # the (collapsed) card is unaffected.
    def handle_sun_overlay(route: Route) -> None:
        body = {
            "available": True,
            "date": "2026-07-30",
            "modelled_pr": pv["performance_ratio"],
            "points": [],
            "excluded": [],
            "excluded_coverage": 0,
            "excluded_no_data": 0,
            "system": _pv_system_block(pv),
        }
        if sun_overlay:
            body.update(sun_overlay)
        route.fulfill(status=200, content_type="application/json", body=json_body(body))

    page.route("**/api/energy/sun-overlay*", handle_sun_overlay)


@pytest.fixture
def mock_energy(page: Page) -> Callable[..., None]:
    """Stub the energy endpoints with deterministic fixtures.

    Covers the live snapshot (``/api/energy``), today's totals
    (``/api/energy/today``), the live-chart history (``/api/energy/history``),
    the history buckets (``/api/energy/aggregate``), the tiered cost & savings
    breakdown (``/api/energy/cost``), the solar forecast
    (``/api/energy/forecast``), the PV-system config it is computed from
    (``/api/energy/pv-system``, issue #561), the read-only sun-position
    diagnostic (``/api/energy/sun-overlay``, issue #590) and the fleet
    solar-boost sequencing knobs (``/api/hvac/boost-coordinator``, issue #562 —
    an HVAC path whose card lives on this tab). Call before navigating. Defaults
    describe a sunny exporting moment so the flow row, charts, and cost table
    have content.

    The PV-system route is **stateful**: a PUT updates the in-memory rows, and
    the forecast's ``system`` block is derived from them, so a test can assert
    that saving the editor is reflected back in the forecast card. Stubbing
    both also keeps any Energy-tab test off the network — an unstubbed forecast
    would have the disposable instance call Open-Meteo for real.
    """
    def _install(
        snapshot: Optional[Dict] = None,
        samples: Optional[List[Dict]] = None,
        buckets: Optional[List[Dict]] = None,
        today: Optional[Dict] = None,
        cost: Optional[Dict] = None,
        pv_arrays: Optional[List[Dict]] = None,
        boost_coord: Optional[Dict] = None,
        forecast: Optional[Dict] = None,
        sun_overlay: Optional[Dict] = None,
        today_gap_hours: float = 0.0,
    ) -> None:
        _route_live_series(
            page,
            snapshot or _default_snapshot(),
            samples if samples is not None else _default_history(),
            buckets if buckets is not None else _default_buckets(),
            today if today is not None else _default_today(),
            today_gap_hours,
        )
        _json_route(page, "**/api/energy/cost*", cost if cost is not None else _default_cost())
        _route_export_rates(page)
        pv = _route_pv_system(page, pv_arrays)
        _route_boost_coordinator(page, boost_coord)
        _route_forecast(page, pv, forecast)
        _route_sun_overlay(page, pv, sun_overlay)

    return _install
