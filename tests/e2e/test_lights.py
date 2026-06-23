"""Elgato Lights tab: render, on/off, brightness, and warmth controls."""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


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
    page.locator("#tabLights").click()
    page.wait_for_selector("#paneLights", state="visible")


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

    expect(page.locator("#tabLights .tab-label")).to_have_text("Light")
    expect(page.locator("#tabNetwork .tab-label")).to_have_text("Net")
    expect(page.locator("#tabSecurity .tab-label")).to_have_text("Alarm")
    expect(page.locator(".light-card")).to_have_count(2)
    expect(page.locator("#lightsGrid")).to_contain_text("Fixture Key Light")
    offline = page.locator('[data-light-id="192.0.2.11:9123"]')
    expect(offline).to_have_class("card light-card is-unavailable")
    expect(offline.locator(".light-unavailable")).to_contain_text("timed out")


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

    card = page.locator('[data-light-id="192.0.2.10:9123"]')
    toggle = card.locator(".toggle")
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(card.locator(".toggle")).to_have_attribute("aria-checked", "false")

    brightness = card.locator('input[aria-label="Brightness exact value for Fixture Key Light"]')
    brightness.fill("55")
    brightness.dispatch_event("change")
    expect(card.locator(".light-control-value").first).to_have_text("55%")

    card = page.locator('[data-light-id="192.0.2.10:9123"]')
    warmth = card.locator('input[aria-label="Warmth exact value for Fixture Key Light"]')
    expect(warmth).to_have_value("5000")
    warmth.fill("4000")
    warmth.dispatch_event("change")
    expect(card.locator(".light-control-value").nth(1)).to_have_text("4000 K")


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
    page.locator("#tabLights").click()
    page.wait_for_selector("#paneLights", state="visible")

    page.get_by_test_id("lights-all-off").click()
    assert store[0]["on"] is False
    assert store[1]["on"] is False
    page.get_by_test_id("lights-all-on").click()
    assert store[0]["on"] is True
    assert store[1]["on"] is False

    card = page.locator('[data-light-id="192.0.2.10:9123"]')
    card.locator(".light-name").click()
    expect(page.locator("#lightDialog")).to_be_visible()
    expect(page.locator("#lightOriginalName")).to_have_text("Fixture Key Light")
    expect(page.locator("#lightProduct")).to_have_text("Elgato Key Light")
    expect(page.locator("#lightHost")).to_have_text("192.0.2.10")
    expect(page.locator("#lightPort")).to_have_text("9123")
    expect(page.locator("#lightMac")).to_have_text("AA:BB:CC:DD:EE:FF")

    page.locator("#lightDisplayName").fill("Desk left")
    page.locator("#lightDisplayName").press("Enter")
    expect(page.locator("#lightDetailName")).to_have_text("Desk left")
    expect(card.locator(".light-name")).to_have_text("Desk left")
