"""Tesla fixtures: sample ``/api/tesla``, ``/history`` and ``/sessions`` bodies and their stubs."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Callable, Dict

import pytest
from playwright.sync_api import Page

from tests.e2e.fixtures._routes import json_body


@pytest.fixture
def sample_tesla() -> Dict:
    """A configured car, awake and charging at home, read five minutes ago."""
    now = int(time.time())
    return {
        "configured": True,
        "sampler_enabled": True,
        "usable_kwh": 60,
        "sample": {
            "ts": now - 300,
            "battery_pct": 72.4,
            "range_km": 351.6,
            "charging_state": "Charging",
            "charger_power_kw": 7.2,
            "charge_energy_added_kwh": 12.4,
            "odometer_km": 15234.5,
            "shift_state": None,
            "speed_kmh": None,
            "at_home": True,
        },
        "state": "online",
        "checked_at": now - 300,
    }


@pytest.fixture
def mock_tesla(page: Page) -> Callable[[Dict], None]:
    """Stub ``GET /api/tesla`` on ``page`` with ``body`` (over the autouse stub)."""
    def _install(body: Dict) -> None:
        page.route(
            "**/api/tesla",
            lambda route: route.fulfill(status=200, content_type="application/json", body=json_body(body)),
        )
    return _install


def _period_start(period: str, back: int = 0) -> str:
    """The local calendar start of the current week (Monday) or month, ``back`` periods ago.

    The browser runs on this machine, so its local calendar is this one.
    """
    today = date.today()
    if period == "week":
        return (today - timedelta(days=today.weekday() + 7 * back)).isoformat()
    month = today.month - 1 - back
    return date(today.year + month // 12, month % 12 + 1, 1).isoformat()


def _bucket(start: str, km: float, drive_kwh: float, charged: float, home: float,
            drives: int, sessions: int) -> Dict:
    return {
        "start": start + "T00:00:00+01:00", "km": km, "drive_kwh": drive_kwh, "drives": drives,
        "charged_kwh": charged, "charged_home_kwh": home, "charging_sessions": sessions,
        "kwh_per_100km": round(drive_kwh / km * 100, 1) if km and drive_kwh else None,
    }


@pytest.fixture
def sample_tesla_history() -> Callable[[str], Dict]:
    """A history body per period: this period and two before it, one with no charging."""
    def _body(period: str) -> Dict:
        return {
            "configured": True, "sampler_enabled": True, "period": period,
            "buckets": [
                _bucket(_period_start(period, 2), 120.0, 18.0, 0.0, 0.0, 4, 0),
                _bucket(_period_start(period, 1), 310.5, 49.7, 52.3, 40.1, 9, 3),
                _bucket(_period_start(period), 214.0, 34.2, 38.4, 30.2, 6, 2),
            ],
        }
    return _body


@pytest.fixture
def sample_tesla_sessions() -> Dict:
    """Two charging sessions (home, then away) and one drive, newest first."""
    now = int(time.time())
    return {
        "configured": True,
        "charging": [
            {"start_ts": now - 7200, "end_ts": now - 3600, "kwh_added": 9.1, "peak_kw": 7.4,
             "start_pct": 70.0, "end_pct": 85.0, "location": "home", "source": "car"},
            {"start_ts": now - 90000, "end_ts": now - 88000, "kwh_added": 29.3, "peak_kw": 120.0,
             "start_pct": 20.0, "end_pct": 70.0, "location": "away", "source": "car"},
        ],
        "drives": [
            {"start_ts": now - 10800, "end_ts": now - 9000, "km": 25.0, "start_pct": 80.0,
             "end_pct": 70.0, "energy_kwh": 6.0},
        ],
    }


@pytest.fixture
def mock_tesla_history(page: Page) -> Callable[..., None]:
    """Stub ``GET /api/tesla/history`` (per period) and ``/sessions`` on ``page``."""
    def _install(history: Callable[[str], Dict], sessions: Dict) -> None:
        def _history(route) -> None:
            period = "month" if "period=month" in route.request.url else "week"
            route.fulfill(status=200, content_type="application/json", body=json_body(history(period)))

        page.route("**/api/tesla/history*", _history)
        page.route(
            "**/api/tesla/sessions*",
            lambda route: route.fulfill(status=200, content_type="application/json", body=json_body(sessions)),
        )
    return _install
