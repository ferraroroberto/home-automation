"""Tesla fixtures: a sample ``/api/tesla`` body and its stub."""

from __future__ import annotations

import time
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
