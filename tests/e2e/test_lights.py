"""Elgato lights (the Devices tab's Lights group): render, on/off, and the
light sheet's brightness and warmth (#884, decision 5 of #872)."""

from __future__ import annotations

import copy
import re
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import hold_reads


@pytest.fixture(autouse=True)
def _no_tuya_lights(mock_tuya: Callable) -> None:
    """The Lights card also lists Tuya lights (#181); stub GET /api/tuya empty
    so these Elgato tests never read the real devices.json."""
    mock_tuya([])


def _open_lights(page: Page) -> None:
    """Devices tab: the Lights group is an open group since #884."""
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")


def _boot_lights(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_lights(sample_lights)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    _open_lights(page)


def test_lights_tab_renders_reachable_and_offline_lights(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    _boot_lights(page, base_url, sample_units, sample_lights, mock_api, mock_energy, mock_lights)

    # Five tabs (#779): Light folded into IoT (#136), shown as "Devices"; Net
    # became the header gear's Settings pane.
    expect(page.locator("#tabIot .tab-label")).to_have_text("Devices")
    expect(page.locator("#tabSecurity .tab-label")).to_have_text("Security")
    expect(page.locator(".tabs .tab")).to_have_count(5)
    expect(page.locator("#tabNetwork")).to_have_count(0)
    expect(page.locator("#tabLights")).to_have_count(0)
    expect(page.locator(".light-row")).to_have_count(2)
    expect(page.locator("#lightsList")).to_contain_text("Fixture Key Light")
    expect(page.locator("#lightsCount")).to_have_text("1 on")
    expect(page.locator('[data-light-id="192.0.2.10:9123"] .action-row-meta-text')).to_have_text("42% · 5000 K")
    offline = page.locator('[data-light-id="192.0.2.11:9123"]')
    expect(offline).to_have_class(re.compile(r"\bis-unavailable\b"))
    expect(offline.locator(".row-avatar")).to_have_attribute("data-badge", "down")
    # Sanitized failure copy (#879): the row says the light is offline and
    # never prints its connection error, which carries the device address.
    expect(offline.locator(".chip")).to_have_text("Offline")
    expect(offline.locator(".toggle")).to_have_count(0)
    expect(offline).not_to_contain_text("timed out")
    expect(offline).not_to_contain_text("192.0.2.11")


def test_lights_tab_distinguishes_loading_from_true_empty(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/lights",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"lights": []}',
        ),
    )
    release = hold_reads(page, "/api/lights")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    _open_lights(page)

    expect(page.locator("#lightsList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#lightsList .empty-state-message")).to_have_text(
        "Reading lights…"
    )
    release()
    expect(page.locator("#lightsList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#lightsList .empty-state-message")).to_have_text(
        "No lights configured or discovered"
    )


def test_lights_tab_shows_contextual_unavailable_state(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/lights",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"raw host 192.0.2.10 timed out after 10 seconds"}',
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    _open_lights(page)

    expect(page.locator("#lightsList")).to_have_attribute("data-state", "error")
    expect(page.locator("#lightsList .empty-state-message")).to_have_text(
        "Lights unavailable"
    )
    expect(page.locator("#lightsNote")).to_have_text(
        "Live light data is unavailable. Check the light connection, then retry."
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.10")


def test_lights_controls_round_trip(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    _boot_lights(page, base_url, sample_units, sample_lights, mock_api, mock_energy, mock_lights)

    row = page.locator('[data-light-id="192.0.2.10:9123"]')
    toggle = row.locator(".toggle")
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(row.locator(".toggle")).to_have_attribute("aria-checked", "false")

    # Tapping the row opens the light sheet: power, brightness and warmth,
    # each applied as it changes (an instant sheet, Done only closes).
    row.locator(".action-row-main").click()
    sheet = page.locator("#lightSheet")
    expect(sheet).to_be_visible()
    expect(sheet).to_have_attribute("data-save-model", "instant")
    expect(page.locator("#lightSheetPower")).to_have_attribute("aria-checked", "false")
    page.locator("#lightSheetPower").click()
    expect(page.locator("#lightSheetPower")).to_have_attribute("aria-checked", "true")
    expect(row.locator(".toggle")).to_have_attribute("aria-checked", "true")

    brightness = sheet.locator('input[aria-label="Brightness exact value for Fixture Key Light"]')
    brightness.fill("55")
    brightness.dispatch_event("change")
    expect(brightness).to_have_value("55")
    expect(sheet.locator(".light-value-edit")).to_have_count(2)

    warmth = sheet.locator('input[aria-label="Warmth exact value for Fixture Key Light"]')
    expect(warmth).to_have_value("5000")
    warmth.fill("4000")
    warmth.dispatch_event("change")
    expect(warmth).to_have_value("4000")
    expect(row.locator(".action-row-meta-text")).to_have_text("55% · 4000 K")
    page.locator("#lightSheetDone").click()
    expect(sheet).to_be_hidden()


def test_lights_bulk_buttons_follow_reachable_state_and_show_progress(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    lights = copy.deepcopy(sample_lights)
    lights[1].update(
        {
            "name": "Fixture Strip",
            "product_name": "Elgato Light Strip",
            "reachable": True,
            "error": None,
            "on": False,
            "brightness": 75,
        }
    )
    store = mock_lights(lights)
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    _open_lights(page)

    all_on = page.get_by_test_id("lights-all-on")
    all_off = page.get_by_test_id("lights-all-off")
    expect(all_on).to_be_enabled()
    expect(all_off).to_be_enabled()

    all_on.click()
    expect(page.locator("#toast")).to_have_text("Activating 1 light")
    expect(page.locator("#toast")).to_have_text("Fixture Strip on")
    expect(all_on).to_be_disabled()
    expect(all_off).to_be_enabled()
    assert store[0]["on"] is True
    assert store[1]["on"] is True

    all_off.click()
    expect(page.locator("#toast")).to_have_text("Deactivating 2 lights")
    expect(page.locator("#toast")).to_have_text("Fixture Strip off")
    expect(all_on).to_be_enabled()
    expect(all_off).to_be_disabled()
    assert store[0]["on"] is False
    assert store[1]["on"] is False


def test_lights_bulk_controls_and_detail_rename(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    store = mock_lights(sample_lights)
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    _open_lights(page)

    expect(page.get_by_test_id("lights-all-on")).to_be_disabled()
    expect(page.get_by_test_id("lights-all-off")).to_be_enabled()
    page.get_by_test_id("lights-all-off").click()
    expect(page.locator("#toast")).to_have_text("Fixture Key Light off")
    expect(page.get_by_test_id("lights-all-on")).to_be_enabled()
    expect(page.get_by_test_id("lights-all-off")).to_be_disabled()
    assert store[0]["on"] is False
    assert store[1]["on"] is False
    page.get_by_test_id("lights-all-on").click()
    expect(page.locator("#toast")).to_have_text("Fixture Key Light on")
    expect(page.get_by_test_id("lights-all-on")).to_be_disabled()
    expect(page.get_by_test_id("lights-all-off")).to_be_enabled()
    assert store[0]["on"] is True
    assert store[1]["on"] is False

    row = page.locator('[data-light-id="192.0.2.10:9123"]')
    row.locator(".action-row-main").click()
    expect(page.locator("#lightSheetEditMeta")).to_have_text("Elgato Key Light")
    page.locator("#lightSheetEdit").click()
    expect(page.locator("#lightDialog")).to_be_visible()
    expect(page.locator("#lightOriginalName")).to_have_text("Fixture Key Light")
    expect(page.locator("#lightProduct")).to_have_text("Elgato Key Light")
    expect(page.locator("#lightHost")).to_have_text("192.0.2.10")
    expect(page.locator("#lightPort")).to_have_text("9123")
    expect(page.locator("#lightMac")).to_have_text("AA:BB:CC:DD:EE:FF")
    expect(page.locator("#lightFirmware")).to_have_text("1.0")
    expect(page.locator("#lightTemperatureMeta")).to_have_text("200 mired · 5000 K")
    expect(page.locator("#lightIdentifier")).to_have_text("192.0.2.10:9123")

    page.locator("#lightDisplayName").fill("Desk left")
    page.locator("#lightDisplayName").press("Enter")
    expect(page.locator("#lightDetailName")).to_have_text("Desk left")
    expect(row.locator(".action-row-title")).to_have_text("Desk left")
    expect(page.locator("#lightSheetName")).to_have_text("Desk left")


def test_lights_refresh_failure_keeps_partial_data_note(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    sample_lights: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_lights: Callable,
) -> None:
    _boot_lights(page, base_url, sample_units, sample_lights, mock_api, mock_energy, mock_lights)

    page.route(
        "**/api/lights/refresh",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"No Elgato lights found. Add ELGATO_LIGHT_HOSTS=host[:9123] to .env."}',
        ),
    )

    page.get_by_test_id("lights-refresh").click()

    expect(page.locator(".light-row")).to_have_count(2)
    expect(page.locator("#lightsList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#lightsNote")).to_contain_text("Last updated")
    expect(page.locator("#lightsNote")).to_contain_text("live data unavailable")
    expect(page.locator("#lightsNote")).not_to_contain_text("ELGATO_LIGHT_HOSTS")
