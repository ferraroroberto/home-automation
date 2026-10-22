"""The nine static detail-modal switches keep one on/off contract (#912).

Plug, detector (Hidden + Ignore trouble), presence entity, attached device
(Important + Hidden), Wi-Fi radio and circuit (Invert + Hidden) dialogs each
carry a switch button that is in index.html, not built by ``buildToggle``.
They used to repaint themselves with a hand-copied body; they now go through
``toggle.js``'s ``setToggleState``. These pin what the repaint must keep: the
``on`` class, ``aria-checked`` and the ON/OFF label agree, flip with a tap and
flip back, and the knob markup survives the repaint.

DOM-state only, so ``chromium_only``. Switches are tapped only inside a
staged or instant dialog against stubbed APIs: no real plug, network setting,
detector or circuit is ever written (a staged switch writes nothing until
Save, and nothing here presses Save; the Wi-Fi switch is the one instant
switch and its PUT is stubbed).
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e._app import boot_home
from tests.e2e.test_circuits import _boot_circuits, _meter
from tests.e2e.test_network import open_network_sheet


def _assert_switch(page: Page, selector: str, on: bool) -> None:
    btn = page.locator(selector)
    if on:
        expect(btn).to_have_class(re.compile(r"(^|\s)on(\s|$)"))
    else:
        expect(btn).not_to_have_class(re.compile(r"(^|\s)on(\s|$)"))
    expect(btn).to_have_attribute("role", "switch")
    expect(btn).to_have_attribute("aria-checked", "true" if on else "false")
    expect(btn.locator(".toggle-label")).to_have_text("ON" if on else "OFF")
    # to_have_text on a display:none label still reads the DOM text.
    expect(btn.locator(".knob")).to_have_count(1)


def _cycle(page: Page, selector: str) -> None:
    """Off → tap → on → tap → off, with the whole contract checked each time."""
    _assert_switch(page, selector, False)
    page.locator(selector).click()
    _assert_switch(page, selector, True)
    page.locator(selector).click()
    _assert_switch(page, selector, False)


def _json_ok(route: Route) -> None:
    route.fulfill(status=200, content_type="application/json", body="{}")


@pytest.mark.chromium_only
def test_plug_hidden_switch(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya(sample_plugs)
    boot_home(page, base_url)
    page.locator("#tabIot").click()
    page.locator('[data-device-id="plug-1"] .action-row-main').click()
    expect(page.locator("#plugDialog")).to_be_visible()
    _cycle(page, "#plugHiddenToggle")


@pytest.mark.chromium_only
def test_circuit_invert_and_hidden_switches(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])
    # The second channel is a plain, un-inverted, visible one.
    page.locator("#circuitsList .circuit-row .action-row-main").nth(1).click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    _cycle(page, "#circuitInvertToggle")
    _cycle(page, "#circuitHiddenToggle")


@pytest.mark.chromium_only
def test_zone_hidden_and_ignore_trouble_switches(
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
    page.locator("#securityZones .security-zone .action-row-main").first.click()
    expect(page.locator("#zoneDialog")).to_be_visible()
    _cycle(page, "#zoneHiddenToggle")
    _cycle(page, "#zoneTroubleIgnoreToggle")


@pytest.mark.chromium_only
def test_presence_hidden_switch(
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
    page.locator("#presenceList .presence-row .action-row-main").first.click()
    expect(page.locator("#presenceDialog")).to_be_visible()
    _cycle(page, "#presenceHiddenDetailToggle")


@pytest.mark.chromium_only
def test_network_device_and_wifi_switches(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network()
    # The Wi-Fi switch saves on tap (an instant sheet): stub every write.
    page.route("**/api/network/**", lambda route: (
        _json_ok(route) if route.request.method in ("PUT", "POST") else route.fallback()
    ))
    boot_home(page, base_url)
    open_network_sheet(page)

    page.locator("details.net-wifi-card > summary").click()
    page.locator("#netWifiList .net-wifi-row .net-wifi-row-name").first.click()
    expect(page.locator("#netWifiDialog")).to_be_visible()
    _assert_switch(page, "#netWifiHiddenDetailToggle", False)
    page.locator("#netWifiHiddenDetailToggle").click()
    _assert_switch(page, "#netWifiHiddenDetailToggle", True)
    page.locator("#netWifiDetailClose").click()

    page.locator("details.net-devices-card > summary").click()
    page.locator("#netDevices .net-device-name").filter(has_text="Alpha Laptop").click()
    expect(page.locator("#netDeviceDialog")).to_be_visible()
    _cycle(page, "#netDeviceImportant")
    _cycle(page, "#netDeviceHiddenToggle")
