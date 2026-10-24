"""iCloud presence (and the location it reads) API stub."""

from __future__ import annotations

from typing import Callable, Dict, Optional

import pytest
from playwright.sync_api import Page

from tests.e2e.fixtures._routes import json_body


@pytest.fixture
def mock_presence(page: Page) -> Callable[..., None]:
    """Stub the read-only iCloud presence API with deterministic entities."""
    def _install(snapshot: Optional[Dict] = None) -> None:
        body = snapshot or {
            "available": True,
            "total_count": 3,
            "located_count": 2,
            "home_count": 1,
            "away_count": 1,
            "unknown_count": 1,
            "all_away": False,
            "home_radius_m": 200,
            "entities": [
                {
                    "entity_id": "home-phone",
                    "name": "Home Phone",
                    "model": "iPhone",
                    "device_class": "iPhone",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "horizontal_accuracy_m": 8.0,
                    "last_seen": "2026-06-22T10:00:00+00:00",
                    "battery_level_pct": 80,
                    "battery_status": "Charging",
                    "distance_from_home_m": 50.0,
                    "at_home": True,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
                {
                    "entity_id": "away-phone",
                    "name": "Away Phone",
                    "model": "iPhone",
                    "device_class": "iPhone",
                    "latitude": 0.1,
                    "longitude": 0.0,
                    "horizontal_accuracy_m": 12.0,
                    "last_seen": "2026-06-22T09:45:00+00:00",
                    "battery_level_pct": 60,
                    "battery_status": "NotCharging",
                    "distance_from_home_m": 1100.0,
                    "at_home": False,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
                {
                    "entity_id": "tag",
                    "name": "Keys",
                    "model": "AirTag",
                    "device_class": "Accessory",
                    "latitude": None,
                    "longitude": None,
                    "horizontal_accuracy_m": None,
                    "last_seen": None,
                    "battery_level_pct": None,
                    "battery_status": None,
                    "distance_from_home_m": None,
                    "at_home": None,
                    "display_name": None,
                    "hidden": False,
                    "source": "icloud",
                    "stale": False,
                },
            ],
            "diagnostics": {"available": True, "reason": "ok", "detail": "", "refreshed_at": "2026-06-22T10:00:00+00:00"},
            "automation": {"auto_arm_enabled": False, "arm_away_after_s": 900, "stale_after_s": 3600, "auto_disarm_enabled": False},
        }
        page.route("**/api/presence", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json_body(body)))
        page.route("**/api/location", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json_body({"lat": 0.0, "lon": 0.0, "label": "Home"})))
        page.route("**/api/location/reverse*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json_body({"available": True, "label": "Fixture Place"})))

    return _install
