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

    brightness = card.locator('input[aria-label="Brightness for Fixture Key Light"]')
    brightness.evaluate("(el) => { el.value = 55; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
    expect(card.locator(".light-control-value").first).to_have_text("55%")

    warmth = card.locator('input[aria-label="Warmth for Fixture Key Light"]')
    warmth.evaluate("(el) => { el.value = 4000; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
    expect(card.locator(".light-control-value").nth(1)).to_have_text("4000 K")
