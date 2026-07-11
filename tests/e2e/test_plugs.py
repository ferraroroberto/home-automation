"""Smart Life (Plugs) tab: split Plugs/Blinds row lists, wattage, switch + covers.

Drives the Plugs tab against stubbed local-Tuya fixtures (no LAN, no cloud) on
both the Chromium-desktop and WebKit/iPhone projections. Plugs and blinds render
as compact divider-separated rows inside two collapsible cards (collapsed by
default); these tests expand them, then cover a metered plug row, a switch
round-trip, the blind icon controls, an unavailable device that must not block
the reachable ones, and the reachable-only toggle for no-IP adapters.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot_plugs(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    # Stub units + energy too so boot doesn't touch the real cloud, then open
    # the Plugs tab (lazy-loads GET /api/tuya on entry).
    mock_api(sample_units)
    mock_energy()
    mock_tuya(sample_plugs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabPlugs").click()
    page.wait_for_selector("#panePlugs", state="visible")
    # Both list cards are collapsed by default — expand them so their rows are
    # visible/interactable (open persists across re-renders).
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )


def test_plugs_tab_renders_all_devices(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # One row per device, split across the two list cards.
    expect(page.locator(".device-row")).to_have_count(len(sample_plugs))
    expect(page.locator("#plugsList")).to_contain_text("Test Heater")
    expect(page.locator("#blindsList")).to_contain_text("Test Blind")


def test_plugs_distinguish_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/tuya' || url.endsWith('/api/tuya')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabPlugs").click()

    expect(page.locator("#plugsFeedback")).to_have_attribute("data-state", "loading")
    expect(page.locator("#plugsFeedback .empty-state-message")).to_have_text(
        "Reading plugs and blinds…"
    )
    expect(page.locator("#plugsFeedback")).to_have_attribute("data-state", "empty")
    expect(page.locator("#plugsFeedback .empty-state-message")).to_have_text(
        "No Smart Life devices configured"
    )


def test_plugs_show_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    page.route(
        "**/api/tuya",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"device 192.0.2.60 timed out after 10 seconds"}',
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabPlugs").click()

    expect(page.locator("#plugsFeedback")).to_have_attribute("data-state", "error")
    expect(page.locator("#plugsFeedback .empty-state-message")).to_have_text(
        "Plugs and blinds unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.60")


def test_plug_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(
        page, base_url, sample_units, sample_plugs,
        mock_api, mock_energy, mock_tuya,
    )
    expect(page.locator(".device-row")).to_have_count(len(sample_plugs))

    page.route(
        "**/api/tuya",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"device 192.0.2.60 timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabHome").click()
    page.locator("#tabPlugs").click()

    expect(page.locator("#plugsFeedback")).to_have_attribute("data-state", "stale")
    expect(page.locator(".device-row")).to_have_count(len(sample_plugs))
    expect(page.locator("#plugsFeedback")).to_contain_text("Last updated")
    expect(page.locator("#plugsFeedback")).to_contain_text("live data unavailable")
    expect(page.locator("#plugsFeedback")).not_to_contain_text("192.0.2.60")


def test_ups_distinguishes_loading_from_not_detected(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/ups' || url.endsWith('/api/ups')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({
                  ups: {available: false, source: 'none', error: null}
                }), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")

    expect(page.locator("#homeUpsTile")).to_have_attribute("data-state", "loading")
    expect(page.locator("#homeUpsTile .empty-state-message")).to_have_text(
        "Reading UPS status…"
    )
    expect(page.locator("#homeUpsTile")).to_have_attribute("data-state", "empty")
    expect(page.locator("#homeUpsTile .empty-state-message")).to_have_text(
        "No UPS detected"
    )


def test_ups_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/ups",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"nut host 192.0.2.70 timed out after 10 seconds"}',
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")

    expect(page.locator("#homeUpsTile")).to_have_attribute("data-state", "error")
    expect(page.locator("#homeUpsTile .empty-state-message")).to_have_text(
        "UPS status unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.70")


def test_ups_poll_failure_preserves_last_good_status(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    failing = {"value": False}
    ups = {
        "available": True,
        "source": "nut",
        "status": "online",
        "mains_online": True,
        "battery_charge_pct": 90,
        "runtime_seconds": 3600,
        "alarms": [],
    }

    def handle_ups(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"nut host 192.0.2.70 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ups": ups}),
        )

    page.route("**/api/ups", handle_ups)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    expect(page.locator("#homeUpsTile")).to_have_attribute("data-state", "ready")
    expect(page.locator("#homeUpsTile")).to_contain_text("90%")

    failing["value"] = True
    page.locator("#tabAc").click()
    page.locator("#tabHome").click()

    expect(page.locator("#homeUpsTile")).to_have_attribute("data-state", "stale")
    expect(page.locator("#homeUpsTile")).to_contain_text("90%")
    expect(page.locator("#homeUpsTile")).to_contain_text("Last updated")
    expect(page.locator("#homeUpsTile")).to_contain_text("live data unavailable")
    expect(page.locator("#homeUpsTile")).not_to_contain_text("192.0.2.70")


def test_metered_plug_shows_watts(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Wattage is a first-class value on the metered plug row. Grouped digits
    # per the shared fmtW (format.js, #383) — one watt format across tabs.
    watts = page.locator('[data-device-id="plug-1"] .plug-watts')
    expect(watts).to_be_visible()
    expect(watts).to_have_text("1,450 W")


def test_plugs_stats_block_summarizes(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # 4 devices; Heater on, Lamp off; live watts = the metered, reachable plug.
    expect(page.locator("#plugsStats")).to_be_visible()
    expect(page.locator("#plugStatTotal")).to_have_text("4")
    expect(page.locator("#plugStatOn")).to_have_text("1")
    expect(page.locator("#plugStatOff")).to_have_text("1")
    expect(page.locator("#plugStatWatts")).to_have_text("1,450 W")


def test_plug_rename_round_trips(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Tap the name → rename modal opens; saving relabels the row from the override.
    page.locator('[data-device-id="plug-1"] .device-row-name').click()
    expect(page.locator("#plugDialog")).to_be_visible()
    field = page.locator("#plugDisplayName")
    field.fill("Garage Heater")
    field.press("Enter")  # Enter blurs → PUT /api/tuya/{id}/display_name
    expect(page.locator('[data-device-id="plug-1"] .device-row-name')).to_have_text("Garage Heater")


def test_switch_toggle_round_trips(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Test Lamp starts OFF; clicking its toggle flips it ON via the read-back.
    toggle = page.locator('[data-device-id="plug-2"] .toggle')
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()
    expect(page.locator('[data-device-id="plug-2"] .toggle')).to_have_attribute(
        "aria-checked", "true"
    )


def test_blind_has_icon_controls(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # The blind lives in the Blinds card with three up/stop/down icon buttons.
    buttons = page.locator('[data-device-id="cover-1"] .blind-btn')
    expect(buttons).to_have_count(3)
    boxes = buttons.evaluate_all("""
        controls => controls.map(control => {
          const rect = control.getBoundingClientRect();
          return {left: rect.left, right: rect.right, width: rect.width, height: rect.height};
        })
    """)
    assert all(box["width"] == 44 and box["height"] == 44 for box in boxes)
    assert all(boxes[index]["right"] <= boxes[index + 1]["left"] for index in range(2))
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # Open is actionable and does not raise (stub acks the action).
    page.locator('[data-device-id="cover-1"] .blind-btn[data-action="open"]').click()


def test_offline_device_unavailable_without_blocking_others(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    offline = page.locator('[data-device-id="plug-3"]')
    expect(offline).to_have_class("device-row plug-row is-unavailable")
    expect(offline.locator(".plug-unavailable")).to_be_visible()
    # The offline row has no power toggle, but the reachable plug still does.
    expect(offline.locator(".toggle")).to_have_count(0)
    expect(page.locator('[data-device-id="plug-1"] .toggle')).to_be_visible()


def test_default_view_shows_no_ip_adapters(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs_with_no_ip: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    """By default source-visible no-IP adapters render with an unavailable reason."""
    _boot_plugs(
        page, base_url, sample_units, sample_plugs_with_no_ip,
        mock_api, mock_energy, mock_tuya,
    )

    no_ip = page.locator('[data-device-id="plug-noip"]')
    expect(no_ip).to_be_visible()
    # Compact status word in the row; the full reason lives in the hover title.
    note = no_ip.locator(".plug-unavailable")
    expect(note).to_have_text("No IP")
    expect(note).to_have_attribute("title", re.compile("tinytuya snapshot"))
    expect(page.locator(".device-row")).to_have_count(5)
    expect(page.locator("#plugsHiddenCount")).to_be_hidden()


def test_reachable_only_toggle_hides_no_ip_adapters(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs_with_no_ip: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    """Clicking the toggle hides no-IP adapters; clicking again restores them."""
    _boot_plugs(
        page, base_url, sample_units, sample_plugs_with_no_ip,
        mock_api, mock_energy, mock_tuya,
    )

    toggle = page.locator('[data-testid="plugs-show-all-toggle"]')
    expect(toggle).to_be_visible()

    # Default: no-IP visible.
    expect(page.locator('[data-device-id="plug-noip"]')).to_have_count(1)

    # Click "Reachable only" → no-IP hidden.
    toggle.click()
    expect(page.locator(".device-row")).to_have_count(4)
    expect(page.locator('[data-device-id="plug-noip"]')).to_have_count(0)
    expect(page.locator("#plugsHiddenCount")).to_contain_text("1 no-IP hidden")

    # Click again ("Show all devices") → back to 5.
    toggle.click()
    expect(page.locator(".device-row")).to_have_count(5)
    expect(page.locator('[data-device-id="plug-noip"]')).to_have_count(1)


def test_refresh_button_reloads_plugs(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    page.get_by_test_id("plugs-refresh").click()
    expect(page.locator("#toast")).to_have_text("Plugs refreshed")
