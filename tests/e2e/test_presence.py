"""Presence in the Security pane — rows, states, settings layout, this-device.

Loading vs true-empty, contextual unavailability, stale-preserving refresh
failures, the compact right-aligned settings controls, and the diagnostic-only
"This device" row.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home


def test_presence_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence({
        "available": True,
        "total_count": 0,
        "located_count": 0,
        "home_count": 0,
        "away_count": 0,
        "unknown_count": 0,
        "all_away": False,
        "home_radius_m": 200,
        "entities": [],
        "diagnostics": {
            "available": True,
            "reason": "ok",
            "detail": "",
            "refreshed_at": "2026-06-22T10:00:00+00:00",
        },
    })
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/presence' || url.endsWith('/api/presence')) {
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

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "Reading presence…"
    )
    expect(page.locator("#presenceList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "No presence entities configured"
    )


def test_presence_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/presence",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"icloud.example.internal timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "error")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "Presence unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("icloud.example.internal")


def test_presence_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    expect(page.locator("#presenceList .presence-row")).to_have_count(3)

    page.route(
        "**/api/presence",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"icloud.example.internal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabHome").click()
    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#presenceList .presence-row")).to_have_count(3)
    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator("#presenceNote")).to_contain_text("Last updated")
    expect(page.locator("#presenceNote")).to_contain_text("live data unavailable")
    expect(page.locator("#presenceKidsHome")).to_be_disabled()
    expect(page.locator("#presenceNote")).not_to_contain_text(
        "icloud.example.internal"
    )


def test_presence_settings_use_compact_right_aligned_controls(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("details.presence-card > summary").click()
    page.locator("details.presence-settings-card > summary").click()

    control_ids = [
        "locationLabel",
        "locationLat",
        "locationLon",
        "presenceAutoEnabled",
        "presenceArmMinutes",
        "presenceStaleMinutes",
        "presenceDisarmOnArrival",
    ]
    boxes = {
        control_id: page.locator(f"#{control_id}").bounding_box()
        for control_id in control_ids
    }
    assert all(box is not None for box in boxes.values())
    right_edges = {
        round(box["x"] + box["width"])
        for box in boxes.values()
        if box is not None
    }
    assert len(right_edges) == 1, boxes

    assert boxes["locationLabel"]["width"] == boxes["locationLat"]["width"]
    assert boxes["locationLabel"]["width"] == boxes["locationLon"]["width"]
    assert boxes["locationLat"]["width"] <= 144
    assert boxes["locationLon"]["width"] <= 144
    assert boxes["presenceArmMinutes"]["width"] <= 88
    assert boxes["presenceStaleMinutes"]["width"] <= 88

    rows = page.locator(".presence-settings > .row")
    for index in range(rows.count()):
        label_box = rows.nth(index).locator(":scope > span").bounding_box()
        control_box = rows.nth(index).locator(":scope > input, :scope > button").bounding_box()
        assert label_box is not None and control_box is not None
        assert label_box["x"] + label_box["width"] <= control_box["x"] - 8


def test_security_tab_renders_presence_spike(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)

    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_be_visible()
    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator(".presence-row")).to_have_count(3)
    expect(page.locator(".presence-row.is-home")).to_contain_text("Home Phone")
    expect(page.locator(".presence-row.is-away")).to_contain_text("Away Phone")
    expect(page.locator(".presence-row.is-unknown")).to_contain_text("Keys")


def test_this_device_presence_is_diagnostic_only(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.add_init_script("""
        localStorage.setItem('home-automation.thisDevicePresence', 'true');
        localStorage.setItem('home-automation.thisDeviceLocation', JSON.stringify({
          lat: 0,
          lon: 0,
          accuracy: 8,
          last_seen: new Date().toISOString(),
        }));
    """)
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)

    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator(".presence-row")).to_have_count(4)
    expect(page.locator(".presence-row").first).to_contain_text("This device")
    expect(page.locator(".presence-row").first).to_contain_text("Browser GPS · diagnostic only")
    expect(page.locator("#presenceRefreshNote")).to_contain_text("not used for alarm automation")
