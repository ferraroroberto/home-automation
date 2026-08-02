"""Circuits card (issue #25): every CT-clamp channel renders, clamp or not.

Drives the IoT tab's Circuits card against a stubbed ``GET /api/circuits`` (no
mDNS, no meter I/O) on both the Chromium-desktop and WebKit/iPhone projections.

The contract worth a browser test is the one a well-meaning refactor would
quietly break: **a channel reading 0 W is never filtered out**. More clamps get
fitted over time, and a channel that vanishes because it currently measures
nothing is indistinguishable, on screen, from a channel that was never there.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect

METER_ID = "AA:BB:CC:DD:EE:01"


def _channel(number: int, **overrides: object) -> Dict:
    """One channel with nothing measured — the no-clamp-fitted default."""
    channel = {
        "channel": number,
        "key": f"{METER_ID}:{number}",
        "display_name": None,
        "power_w": None,
        "power_raw_w": None,
        "current_a": None,
        "energy_kwh": None,
        "inverted": False,
    }
    channel.update(overrides)
    return channel


def _meter(reachable: bool = True) -> Dict:
    return {
        "meter_id": METER_ID,
        "name": "Athom Energy Monitor ddee01",
        "display_name": None,
        "model": "China Athom Technology.Athom Energy Monitor(6 Channels)",
        "host": "192.0.2.73",
        "reachable": reachable,
        "error": None if reachable else "Offline — no response on the LAN.",
        "voltage_v": 239.4 if reachable else None,
        "frequency_hz": 50.0 if reachable else None,
        "temperature_c": 34.0 if reachable else None,
        "wifi_rssi_dbm": -68 if reachable else None,
        "total_power_w": 291.5 if reachable else None,
        "total_energy_kwh": 6.88 if reachable else None,
        "channels": (
            [
                # 1: a live, sign-corrected clamp. 2: a fitted clamp on an idle
                # circuit (a real 0 W). 3-6: no clamp fitted at all.
                _channel(1, display_name="water heater", power_w=291.5,
                         power_raw_w=-291.5, current_a=1.81, energy_kwh=6.88,
                         inverted=True),
                _channel(2, power_w=0.0, power_raw_w=0.0, current_a=0.0, energy_kwh=0.0),
            ]
            + [_channel(n) for n in range(3, 7)]
            if reachable
            else [_channel(n) for n in range(1, 7)]
        ),
    }


def _boot_circuits(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, meters: List[Dict],
) -> None:
    mock_api(sample_units)
    mock_energy()
    # Overrides the conftest's autouse empty-meters stub.
    page.route(
        "**/api/circuits",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"meters": meters, "discovery_ok": True, "error": None}),
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )


def test_every_channel_renders_even_with_no_clamp_fitted(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    rows = page.locator("#circuitsList .circuit-row")
    expect(rows).to_have_count(6)
    # The named clamp keeps its label; the rest fall back to their terminal
    # number so an unlabelled clamp is still identifiable on the meter.
    expect(rows.nth(0)).to_contain_text("water heater")
    expect(rows.nth(1)).to_contain_text("Clamp 2")
    expect(rows.nth(5)).to_contain_text("Clamp 6")


def test_a_measured_zero_is_shown_and_an_unmeasured_channel_is_not_faked(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    rows = page.locator("#circuitsList .circuit-row")
    # The sign-corrected clamp reports positive watts, not the raw negative.
    expect(rows.nth(0).locator(".plug-watts")).to_have_text("292 W")
    # Channel 2 genuinely measured 0 W.
    expect(rows.nth(1).locator(".plug-watts")).to_have_text("0 W")
    # Channels 3-6 measured nothing — never dressed up as a 0 W reading.
    expect(rows.nth(2).locator(".plug-watts")).to_have_count(0)
    expect(rows.nth(2)).to_contain_text("no reading")


def test_an_offline_meter_keeps_its_channel_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A meter dropping off Wi-Fi must dim its card, not delete circuits."""
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy, [_meter(reachable=False)]
    )

    expect(page.locator("#circuitsList .circuit-row")).to_have_count(6)
    expect(page.locator("#circuitsList .circuit-meter")).to_contain_text("offline")


def test_rename_dialog_shows_the_clamp_flip_only_for_a_channel(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """One dialog serves both; a meter has no clamp direction to correct."""
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    page.locator("#circuitsList .circuit-row .device-row-name").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("water heater")
    expect(page.locator("#circuitInvertSection")).to_be_visible()
    expect(page.locator("#circuitInvertToggle")).to_have_attribute("aria-checked", "true")
    page.locator("#circuitDetailClose").click()

    page.locator("#circuitsList .circuit-meter-name").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("Athom Energy Monitor ddee01")
    expect(page.locator("#circuitInvertSection")).to_be_hidden()
