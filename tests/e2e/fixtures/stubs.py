"""Autouse stubs that keep every browser test off the household's live services."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route


def stub_home_assistant_api(page: Page) -> None:
    """Never let a browser regression test inspect the household's live HA."""

    page.route(
        "**/api/ha",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"satellites":[],"interactions":[],"voice_transcriber":true}',
        ),
    )


def stub_circuits_api(page: Page) -> None:
    """Never let a browser regression test run a live mDNS sweep (issue #25).

    Unstubbed, ``GET /api/circuits`` really browses the LAN, so the suite's
    results would depend on whether a CT-clamp meter happens to be powered on —
    and every discovered channel adds a ``.device-row`` to the IoT tab, which
    silently broke the plug row counts. Same reasoning as the HA stub above.
    Tests that want meters install their own route over this one.
    """

    page.route(
        "**/api/circuits",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"meters":[],"discovery_ok":true,"error":null}',
        ),
    )


def stub_network_api(page: Page) -> None:
    """Never let a browser regression test read the live router and AP (#884).

    The Devices tab reads ``GET /api/network`` on entry since #884 (its
    Network group's rows), and the real read logs in to the router and polls
    the access point. An empty snapshot keeps every Devices test off the LAN;
    tests that want a network install ``mock_network`` over this one.
    """

    def handle(route: Route) -> None:
        if route.request.method != "GET":
            route.fulfill(status=503, content_type="application/json",
                          body='{"detail":"network not stubbed"}')
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"internet":null,"access_point":null,"router":null,"wifi":null,"devices":[]}',
        )

    page.route("**/api/network", handle)
    page.route("**/api/network?*", handle)


def stub_tesla_api(page: Page) -> None:
    """Keep the car card hidden unless a test installs ``mock_tesla``.

    ``GET /api/tesla`` only reads the local store, but whether the boot sees a
    configured car depends on the checkout's ``.env`` and token file; a fixed
    not-configured answer keeps every Devices test independent of that.
    """

    page.route(
        "**/api/tesla",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"configured":false,"sampler_enabled":false,"usable_kwh":60,'
                 '"sample":null,"state":null,"awake":false,"checked_at":null}',
        ),
    )


def install_baseline_stubs(page: Page) -> None:
    """Install every always-on stub on ``page`` (again, after a flow reset)."""
    stub_home_assistant_api(page)
    stub_circuits_api(page)
    stub_network_api(page)
    stub_tesla_api(page)


@pytest.fixture(autouse=True)
def _baseline_stubs(page: Page) -> None:
    install_baseline_stubs(page)
