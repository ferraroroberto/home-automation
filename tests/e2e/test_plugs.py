"""Smart Life (Plugs) tab: split Plugs/Blinds row lists, wattage, switch + covers.

Drives the Plugs tab against stubbed local-Tuya fixtures (no LAN, no cloud) on
both the Chromium-desktop and WebKit/iPhone projections. Plugs and blinds render
as compact divider-separated rows inside two collapsible cards (collapsed by
default); these tests expand them, then cover a metered plug row, a switch
round-trip, the blind icon controls, an unavailable device that must not block
the reachable ones, and the reachable-only toggle for no-IP adapters.
"""

from __future__ import annotations

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


def test_metered_plug_shows_watts(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Wattage is a first-class value on the metered plug row.
    watts = page.locator('[data-device-id="plug-1"] .plug-watts')
    expect(watts).to_be_visible()
    expect(watts).to_have_text("1450 W")


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
    expect(page.locator("#plugStatWatts")).to_have_text("1450 W")


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
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # The blind lives in the Blinds card with three up/stop/down icon buttons.
    buttons = page.locator('[data-device-id="cover-1"] .blind-btn')
    expect(buttons).to_have_count(3)
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
