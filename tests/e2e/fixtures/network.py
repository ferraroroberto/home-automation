"""Network tab API stub: LAN health, devices, Wi-Fi, groups and the walk survey."""

from __future__ import annotations

from typing import Callable, Dict, Optional

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.fixtures._network_data import default_internet_history, default_snapshot
from tests.e2e.fixtures._network_routes import NetworkMock, handle_network


@pytest.fixture
def mock_network(page: Page) -> Callable[..., Dict]:
    """Stub the Network tab API with deterministic LAN health + devices."""
    def _install(
        snapshot: Optional[Dict] = None,
        failures_before_success: int = 0,
        internet_history: Optional[Dict] = None,
    ) -> Dict:
        mock = NetworkMock(
            body=snapshot or default_snapshot(),
            history=internet_history or default_internet_history(),
            failures_before_success=failures_before_success,
        )

        def handle(route: Route) -> None:
            handle_network(mock, route)

        page.route("**/api/network**", handle)
        return mock.body

    return _install
