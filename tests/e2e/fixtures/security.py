"""RISCO security API stub."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._routes import json_body


@pytest.fixture
def mock_security(page: Page) -> Callable[..., None]:
    """Stub the RISCO Security API with a small detector fixture."""
    def _install(snapshot: Optional[Dict] = None) -> None:
        state = snapshot or {
            "reachable": True,
            "label": "Disarmed",
            "mode": "disarmed",
            "supported_actions": ["partial", "perimeter", "arm"],
            "ac_lost": False,
            "assumed_control_panel_state": False,
            "zones": [
                {
                    "id": 1,
                    "name": "Front Door",
                    "type": 1,
                    "status": "closed",
                    "active": False,
                    "bypass": False,
                    "triggered": False,
                    "trouble": False,
                    "display_name": None,
                    "hidden": False,
                },
            ],
        }
        schedule_store: List[Dict] = []

        def handle(route: Route) -> None:
            req = route.request
            url = req.url
            if "/api/security/schedules" in url:
                if req.method == "GET":
                    enabled = [e for e in schedule_store if e.get("enabled") is not False]
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json_body({"enabled": bool(enabled), "count": len(enabled), "entries": schedule_store}),
                    )
                    return
                body = req.post_data_json or {}
                schedule_store[:] = body.get("entries", []) if isinstance(body, dict) else []
                enabled = [e for e in schedule_store if e.get("enabled") is not False]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"enabled": bool(enabled), "count": len(enabled), "entries": schedule_store}),
                )
                return
            if "/api/security/events" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json_body({"events": []}),
                )
                return
            route.fulfill(status=200, content_type="application/json", body=json_body(state))

        page.route("**/api/security**", handle)

    return _install
