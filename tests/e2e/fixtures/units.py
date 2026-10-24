"""Air-conditioning unit fixtures: the sample units and the ``/api/units`` stub."""

from __future__ import annotations

import copy
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._routes import json_body


def _sample_units() -> List[Dict]:
    """Three deterministic fake units. Names are obvious fixtures, never
    the user's real rooms (the repo is public)."""
    modes = ["Heat", "Cool", "Automatic", "Dry"]
    fans = ["Auto", "One", "Two", "Three", "Four", "Five"]
    vert = ["Auto", "Swing", "One", "Two", "Three", "Four", "Five"]
    horiz = ["Auto", "Swing", "Left", "LeftCentre", "Centre", "RightCentre", "Right"]
    ranges = {"Heat": [10, 31], "Cool": [16, 31], "Automatic": [16, 31], "Dry": [16, 31]}
    return [
        {
            "unit_id": "unit-1", "name": "Office", "building": "Test",
            "power": True, "operation_mode": "Cool",
            "room_temperature": 22.5, "set_temperature": 24.0, "fan_speed": "Auto",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": "Auto", "vane_horizontal": "Swing",
            "vane_vertical_options": vert, "vane_horizontal_options": horiz,
            "has_vane_vertical": True, "has_vane_horizontal": True,
        },
        {
            "unit_id": "unit-2", "name": "Studio", "building": "Test",
            "power": False, "operation_mode": "Heat",
            "room_temperature": 19.0, "set_temperature": 21.0, "fan_speed": "Three",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": "Three", "vane_horizontal": None,
            "vane_vertical_options": vert, "vane_horizontal_options": [],
            "has_vane_vertical": True, "has_vane_horizontal": False,
        },
        {
            "unit_id": "unit-3", "name": "Loft", "building": "Test",
            "power": True, "operation_mode": "Automatic",
            "room_temperature": 20.0, "set_temperature": 22.0, "fan_speed": "Auto",
            "operation_modes": modes, "fan_speeds": fans,
            "temp_step": 0.5, "temp_ranges": ranges,
            "vane_vertical": None, "vane_horizontal": None,
            "vane_vertical_options": [], "vane_horizontal_options": [],
            "has_vane_vertical": False, "has_vane_horizontal": False,
        },
    ]


@pytest.fixture
def sample_units() -> List[Dict]:
    return copy.deepcopy(_sample_units())


@pytest.fixture
def mock_api(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Install route stubs for the units API on ``page``.

    ``GET /api/units`` returns the supplied list; ``POST /api/units/{id}``
    merges the JSON body into that unit and echoes it back (mirroring the
    server's read-back). Returns the live list so a test can assert the
    server-bound mutations. Call before navigating.
    """
    def _install(units: List[Dict]) -> List[Dict]:
        store = {u["unit_id"]: u for u in units}
        rule_store: Dict[str, Dict] = {}
        schedule_store: Dict[str, List[Dict]] = {}

        # Map the client's control field names onto the snapshot fields.
        field_map = {
            "set_temperature": "set_temperature",
            "power": "power",
            "operation_mode": "operation_mode",
            "fan_speed": "fan_speed",
            "vane_vertical_direction": "vane_vertical",
            "vane_horizontal_direction": "vane_horizontal",
        }

        def handle(route: Route) -> None:
            req = route.request
            parts = req.url.split("/api/units", 1)[1].strip("/").split("/")
            if parts == [""] or parts == []:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"units": list(store.values())}),
                )
                return

            uid = parts[0]
            if uid not in store:
                route.fulfill(status=404, content_type="application/json",
                              body=json_body({"detail": "not found"}))
                return

            if len(parts) > 1 and parts[1] == "rule":
                if req.method == "GET":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json_body(rule_store.get(uid, {"enabled": False, "cool_target": None, "heat_target": None})))
                    return
                rule_store[uid] = req.post_data_json or {}
                route.fulfill(status=200, content_type="application/json", body=json_body(rule_store[uid]))
                return

            if len(parts) > 1 and parts[1] == "schedule":
                if req.method == "GET":
                    entries = schedule_store.get(uid, [])
                    route.fulfill(status=200, content_type="application/json",
                                  body=json_body({"enabled": any(e.get("enabled") for e in entries), "count": sum(1 for e in entries if e.get("enabled")), "next_time": None, "time": None, "entries": entries}))
                    return
                body = req.post_data_json or {}
                entries = body.get("entries", []) if isinstance(body, dict) else []
                schedule_store[uid] = entries
                enabled = [e for e in entries if e.get("enabled")]
                route.fulfill(status=200, content_type="application/json",
                              body=json_body({"enabled": bool(enabled), "count": len(enabled), "next_time": enabled[0].get("time") if enabled else None, "time": enabled[0].get("time") if enabled else None, "entries": entries}))
                return

            # POST control → merge + echo back the updated snapshot.
            patch = req.post_data_json or {}
            for k, v in patch.items():
                if k in field_map:
                    store[uid][field_map[k]] = v
            route.fulfill(status=200, content_type="application/json", body=json_body(store[uid]))

        page.route("**/api/units", handle)
        page.route("**/api/units/**", handle)
        return list(store.values())

    return _install
