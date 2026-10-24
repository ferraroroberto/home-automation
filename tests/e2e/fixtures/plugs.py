"""Tuya plug fixtures: sample device cards and the ``/api/tuya`` stub."""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._routes import json_body


@pytest.fixture
def sample_plugs() -> List[Dict]:
    """Four deterministic Tuya device cards covering each render branch:
    a metered plug (watts), a plain switch, a cover, and an offline device.
    All four are registered (has_valid_ip=True) so the default filter keeps
    them visible. Names are obvious fixtures, never the user's real devices
    (public repo)."""
    return [
        {
            "device_id": "plug-1", "name": "Test Heater", "category": "cz",
            "has_switch": True, "has_cover": False, "metered": True,
            "has_valid_ip": True, "reachable": True, "switch_on": True,
            "power_w": 1450.0, "current_ma": 6300.0, "voltage_v": 230.0,
            "energy_kwh": 12.5, "error": None,
        },
        {
            "device_id": "plug-2", "name": "Test Lamp", "category": "kg",
            "has_switch": True, "has_cover": False, "metered": False,
            "has_valid_ip": True, "reachable": True, "switch_on": False,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None, "error": None,
        },
        {
            "device_id": "cover-1", "name": "Test Blind", "category": "cl",
            "has_switch": False, "has_cover": True, "metered": False,
            "has_valid_ip": True, "reachable": True, "switch_on": None,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None, "error": None,
        },
        {
            "device_id": "plug-3", "name": "Test Offline", "category": "cz",
            "has_switch": True, "has_cover": False, "metered": True,
            "has_valid_ip": True, "reachable": False, "switch_on": None,
            "power_w": None, "current_ma": None, "voltage_v": None,
            "energy_kwh": None,
            "error": "Offline — refresh devices.json if this persists.",
        },
    ]


@pytest.fixture
def sample_plugs_with_no_ip(sample_plugs: List[Dict]) -> List[Dict]:
    """sample_plugs plus one no-IP adapter (has_valid_ip=False).
    Used to verify stale-address visibility and the reachable-only toggle."""
    import copy
    devices = copy.deepcopy(sample_plugs)
    devices.append({
        "device_id": "plug-noip", "name": "Test NoIP", "category": "cz",
        "has_switch": True, "has_cover": False, "metered": False,
        "has_valid_ip": False, "reachable": False, "switch_on": None,
        "power_w": None, "current_ma": None, "voltage_v": None,
        "energy_kwh": None,
        "error": "No local IP — run `python -m tinytuya snapshot` on the home network, then refresh this tab.",
    })
    return devices


@pytest.fixture
def mock_tuya(page: Page) -> Callable[[List[Dict]], List[Dict]]:
    """Stub the local Tuya API on ``page``.

    ``GET /api/tuya`` returns the supplied device cards; the switch POST flips
    ``switch_on`` and echoes the card back (mirroring the server's read-back);
    the cover POST acknowledges the action, and the group cover POST (#181)
    acknowledges every listed blind. Returns the live list so a test can
    assert server-bound mutations. Call before navigating.
    """
    def _install(devices: List[Dict]) -> List[Dict]:
        store = {d["device_id"]: d for d in devices}

        def handle(route: Route) -> None:
            req = route.request
            if req.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"devices": list(store.values())}),
                )
                return
            # POST .../switch|.../cover or PUT .../display_name on /api/tuya/{id}/{verb}
            parts = req.url.rstrip("/").split("/")
            verb, did = parts[-1], parts[-2]
            if verb == "pair":  # #612 — cloud sync + LAN rescan; "nothing new"
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({
                        "devices": list(store.values()),
                        "pair": {
                            "added": [], "updated": [], "recovered": [],
                            "detail": "No new devices",
                        },
                    }),
                )
                return
            if verb == "covers":  # #181 — group move; every blind accepts
                body = req.post_data_json or {}
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({
                        "action": body.get("action"),
                        "results": [
                            {"device_id": d, "ok": True, "error": None}
                            for d in body.get("device_ids") or []
                        ],
                        "failed": 0,
                    }),
                )
                return
            device = store.get(did)
            if device is None:
                route.fulfill(status=404, content_type="application/json",
                              body=json_body({"detail": "not found"}))
                return
            body = req.post_data_json or {}
            if verb == "display_name":  # PUT — set/clear the override, echo it back
                name = (body.get("display_name") or "").strip()
                device["display_name"] = name or None
                route.fulfill(status=200, content_type="application/json",
                              body=json_body({"device_id": did, "display_name": name or None}))
                return
            if verb == "switch":
                device["switch_on"] = bool(body.get("on"))
                route.fulfill(status=200, content_type="application/json",
                              body=json_body(device))
                return
            if verb == "brightness":  # #870 — echo the card at the new level
                device["brightness_pct"] = int(body.get("brightness"))
                route.fulfill(status=200, content_type="application/json",
                              body=json_body(device))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json_body({"device_id": did, "reachable": True,
                                      "action": body.get("action"), "ok": True}))

        page.route("**/api/tuya", handle)
        page.route("**/api/tuya/**", handle)
        return list(store.values())

    return _install
