"""Cameras list in the Security pane — loading, empty, unavailable, stale."""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home


def test_cameras_distinguish_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"cameras":[]}',
        ),
    )
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/cameras' || url.endsWith('/api/cameras')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Reading cameras…"
    )
    expect(page.locator("#camerasList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "No cameras configured"
    )


def test_cameras_show_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "error")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Cameras unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.50")


def test_camera_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    failing = {"value": False}
    camera = {
        "id": "front-door",
        "display_name": "Front door",
        "reachable": True,
        "model": "Fixture camera",
        "recording": False,
        "ptz_presets": False,
        "ptz_absolute": False,
    }

    def handle_cameras(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"cameras": [camera]}),
        )

    page.route("**/api/cameras", handle_cameras)
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)

    failing["value"] = True
    page.locator("#tabHome").click()
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)
    expect(page.locator("#camerasNote")).to_contain_text("Last updated")
    expect(page.locator("#camerasNote")).to_contain_text("live data unavailable")
    expect(page.locator("#camerasNote")).not_to_contain_text("192.0.2.50")
