"""Elgato light fixtures: sample lights and the ``/api/lights`` stub."""

from __future__ import annotations

from typing import Callable, Dict, List
from urllib.parse import unquote

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._routes import json_body


@pytest.fixture
def sample_lights() -> List[Dict]:
    """Two deterministic Elgato lights: one reachable and one offline."""
    return [
        {
            "light_id": "192.0.2.10:9123",
            "host": "192.0.2.10",
            "port": 9123,
            "name": "Fixture Key Light",
            "display_key": "mac:AA:BB:CC:DD:EE:FF",
            "display_name": None,
            "product_name": "Elgato Key Light",
            "firmware": "1.0",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "on": True,
            "brightness": 42,
            "temperature": 200,
            "temperature_k": 5000,
            "supports_temperature": True,
            "reachable": True,
            "error": None,
        },
        {
            "light_id": "192.0.2.11:9123",
            "host": "192.0.2.11",
            "port": 9123,
            "name": "Fixture Offline",
            "display_key": "192.0.2.11:9123",
            "display_name": None,
            "product_name": None,
            "firmware": None,
            "mac_address": None,
            "on": False,
            "brightness": 0,
            "temperature": 0,
            "temperature_k": 0,
            "supports_temperature": False,
            "reachable": False,
            "error": "192.0.2.11:9123 timed out",
        },
    ]


@pytest.fixture
def mock_lights(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Stub the Elgato lights API on ``page``."""
    def _install(lights: List[Dict]) -> List[Dict]:
        store = {item["light_id"]: item for item in lights}

        def handle(route: Route) -> None:
            req = route.request
            if req.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"lights": list(store.values())}),
                )
                return
            parts = req.url.rstrip("/").split("/")
            verb = parts[-1]
            if verb == "refresh":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"lights": list(store.values()), "refresh": {"safe": True}}),
                )
                return
            light_id = unquote(parts[-2] if verb == "display_name" else verb)
            light = store.get(light_id)
            if light is None:
                route.fulfill(
                    status=404,
                    content_type="application/json",
                    body=json_body({"detail": "not found"}),
                )
                return
            body = req.post_data_json or {}
            if verb == "display_name":
                name = (body.get("display_name") or "").strip()
                light["display_name"] = name or None
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({
                        "light_id": light_id,
                        "display_key": body.get("display_key") or light_id,
                        "display_name": name or None,
                    }),
                )
                return
            if "on" in body:
                light["on"] = bool(body["on"])
            if "brightness" in body:
                light["brightness"] = int(body["brightness"])
            if "temperature_k" in body:
                light["temperature_k"] = int(body["temperature_k"])
                light["temperature"] = round(1_000_000 / light["temperature_k"])
            route.fulfill(status=200, content_type="application/json", body=json_body(light))

        page.route("**/api/lights", handle)
        page.route("**/api/lights/**", handle)
        return list(store.values())

    return _install
