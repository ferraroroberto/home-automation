"""Circuits group (issue #25): every CT-clamp channel renders, clamp or not.

Drives the Devices tab's Circuits group against a stubbed ``GET /api/circuits`` (no
mDNS, no meter I/O) on both the Chromium-desktop and WebKit/iPhone projections.

The contract worth a browser test is the one a well-meaning refactor would
quietly break: **a channel reading 0 W is never filtered out**. More clamps get
fitted over time, and a channel that vanishes because it currently measures
nothing is indistinguishable, on screen, from a channel that was never there.
Issue #619 added a *user*-driven hide on top of that, which makes the
distinction sharper rather than softer: hidden is a decision, 0 W never is.

The rest of #619's shape is here too, because it is all state the DOM holds
rather than the server — the order of meters and clamps is computed
client-side. Since #884 the list is flat (a row per clamp, then a row per
meter, all shared rows from row.js); there is no per-meter fold any more.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect

METER_ID = "AA:BB:CC:DD:EE:01"


def _channel(number: int, meter_id: str = METER_ID, **overrides: object) -> Dict:
    """One channel with nothing measured — the no-clamp-fitted default."""
    channel = {
        "channel": number,
        "key": f"{meter_id}:{number}",
        "display_name": None,
        "power_w": None,
        "power_raw_w": None,
        "current_a": None,
        "energy_kwh": None,
        "inverted": False,
        "hidden": False,
    }
    channel.update(overrides)
    return channel


def _meter(
    reachable: bool = True,
    meter_id: str = METER_ID,
    display_name: object = None,
    channels: object = None,
) -> Dict:
    return {
        "meter_id": meter_id,
        "mac": meter_id,
        "name": "Athom Energy Monitor ddee01",
        "display_name": display_name,
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
        "channels": channels if channels is not None else (
            [
                # 1: a live, sign-corrected clamp. 2: a fitted clamp on an idle
                # circuit (a real 0 W). 3-6: no clamp fitted at all.
                _channel(1, meter_id, display_name="water heater", power_w=291.5,
                         power_raw_w=-291.5, current_a=1.81, energy_kwh=6.88,
                         inverted=True),
                _channel(2, meter_id, power_w=0.0, power_raw_w=0.0,
                         current_a=0.0, energy_kwh=0.0),
            ]
            + [_channel(n, meter_id) for n in range(3, 7)]
            if reachable
            else [_channel(n, meter_id) for n in range(1, 7)]
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
    expect(rows.nth(0).locator(".circuit-watts")).to_have_text("292 W")
    # Channel 2 genuinely measured 0 W.
    expect(rows.nth(1).locator(".circuit-watts")).to_have_text("0 W")
    # Channels 3-6 measured nothing — never dressed up as a 0 W reading.
    expect(rows.nth(2).locator(".circuit-watts")).to_have_text("No reading")


def test_an_offline_meter_keeps_its_channel_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A meter dropping off Wi-Fi must flag its row, not delete circuits."""
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy, [_meter(reachable=False)]
    )

    expect(page.locator("#circuitsList .circuit-row")).to_have_count(6)
    meter_row = page.locator("#circuitsList .circuit-meter")
    expect(meter_row.locator(".chip")).to_have_text("Offline")
    expect(meter_row.locator(".row-avatar")).to_have_attribute("data-badge", "down")
    # The channels carry no readings while the meter is unreachable (never 0 W).
    expect(
        page.locator("#circuitsList .circuit-row .circuit-watts").first
    ).to_have_text("—")


def test_rename_dialog_shows_the_clamp_flip_only_for_a_channel(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """One dialog serves both; a meter has no clamp direction to correct.

    Nor a hidden flag — hiding a meter would hide every circuit under it
    (issue #619).
    """
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    page.locator("#circuitsList .circuit-row .action-row-main").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("water heater")
    expect(page.locator("#circuitInvertSection")).to_be_visible()
    expect(page.locator("#circuitInvertToggle")).to_have_attribute("aria-checked", "true")
    expect(page.locator("#circuitHiddenSection")).to_be_visible()
    page.locator("#circuitDetailClose").click()

    page.locator("#circuitsList .circuit-meter .action-row-main").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("Athom Energy Monitor ddee01")
    expect(page.locator("#circuitInvertSection")).to_be_hidden()
    expect(page.locator("#circuitHiddenSection")).to_be_hidden()


def test_the_meter_row_carries_the_name_alone(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#619: the meter's aggregate left the tab for the dialog.

    This group answers "where is the power going", so the meter's own total,
    mains voltage and Wi-Fi signal are reference figures — not what its row
    should be spending its width on.
    """
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    meter_row = page.locator("#circuitsList .circuit-meter").first
    expect(meter_row.locator(".action-row-title")).to_have_text("Athom Energy Monitor ddee01")
    for reading in ("292 W", "239 V", "-68 dBm", METER_ID):
        expect(meter_row).not_to_contain_text(reading)

    # They are all still reachable, one tap away, plus the meter's MAC.
    meter_row.locator(".action-row-main").click()
    expect(page.locator("#circuitMeterInfo")).to_be_visible()
    expect(page.locator("#circuitMeterVoltage")).to_have_text("239 V")
    expect(page.locator("#circuitMeterTotal")).to_have_text("292 W")
    expect(page.locator("#circuitMeterSignal")).to_have_text("-68 dBm")
    expect(page.locator("#circuitMeterMac")).to_have_text(METER_ID)


def test_a_channels_reference_figures_live_in_the_dialog(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#619: one number per row (watts); amps and kWh moved into the dialog."""
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    row = page.locator("#circuitsList .circuit-row").first
    expect(row).to_contain_text("292 W")
    expect(row).not_to_contain_text("1.81 A")
    expect(row).not_to_contain_text("6.88 kWh")

    row.locator(".action-row-main").click()
    expect(page.locator("#circuitReadings")).to_be_visible()
    expect(page.locator("#circuitReadingPower")).to_have_text("292 W")
    expect(page.locator("#circuitReadingCurrent")).to_have_text("1.81 A")
    expect(page.locator("#circuitReadingEnergy")).to_have_text("6.88 kWh")
    # A meter has no per-clamp readings block of its own.
    page.locator("#circuitDetailClose").click()
    page.locator("#circuitsList .circuit-meter .action-row-main").first.click()
    expect(page.locator("#circuitReadings")).to_be_hidden()


def test_meters_are_ordered_by_name_not_discovery(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A→Z on the visible label, numeric-aware (issue #619).

    mDNS hands meters back in whatever order the sweep saw them, so the only
    way to choose the order of the board is to rename the meters — which only
    works if "2 …" sorts before "10 …" rather than lexically after it. The
    flat list keeps that order twice: every meter's clamps, then the meters.
    """
    meters = [
        _meter(meter_id="AA:BB:CC:DD:EE:10", display_name="10 garage"),
        _meter(meter_id="AA:BB:CC:DD:EE:02", display_name="2 kitchen"),
        _meter(meter_id="AA:BB:CC:DD:EE:01", display_name="1 cuadro principal"),
    ]
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, meters)

    titles = page.locator("#circuitsList .circuit-meter .action-row-title")
    expect(titles).to_have_count(3)
    expect(titles.nth(0)).to_have_text("1 cuadro principal")
    expect(titles.nth(1)).to_have_text("2 kitchen")
    expect(titles.nth(2)).to_have_text("10 garage")

    # The clamps come first, grouped by meter in the same order (6 per meter).
    clamp_metas = page.locator("#circuitsList .circuit-row .action-row-meta-text")
    expect(clamp_metas).to_have_count(18)
    expect(clamp_metas.nth(0)).to_have_text("1 cuadro principal · clamp 1")
    expect(clamp_metas.nth(6)).to_have_text("2 kitchen · clamp 1")
    expect(clamp_metas.nth(12)).to_have_text("10 garage · clamp 1")
    # ... and every clamp row precedes the first meter row.
    rows = page.locator("#circuitsList .action-row")
    expect(rows).to_have_count(21)
    expect(rows.nth(17)).to_have_class(re.compile(r"\bcircuit-row\b"))
    expect(rows.nth(18)).to_have_class(re.compile(r"\bcircuit-meter\b"))


def test_tapping_the_meter_renames_it(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The meter row opens the dialog, and a Save there renames the meter."""
    puts: List[Dict] = []

    def handle(route: Route) -> None:
        req = route.request
        if req.method == "PUT" and req.url.endswith("/display_name"):
            puts.append(req.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")

    # Stubbed so a rename never reaches the real name store.
    page.route("**/api/circuits/**", handle)
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    page.locator("#circuitsList .circuit-meter .action-row-main").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    page.locator("#circuitDisplayName").fill("main board")
    page.locator("#circuitSave").click()

    expect(page.locator("#circuitDetailName")).to_have_text("main board")
    assert puts == [{"display_name": "main board"}], puts
    page.locator("#circuitDetailClose").click()
    expect(
        page.locator("#circuitsList .circuit-meter .action-row-title")
    ).to_have_text("main board")


def test_a_hidden_channel_is_put_away_but_never_lost(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#619: a spare terminal can be hidden — and brought straight back.

    The server keeps returning it either way; only the card stops drawing it.
    """
    channels = [
        _channel(1, display_name="water heater", power_w=291.5, current_a=1.81,
                 energy_kwh=6.88),
        _channel(2, power_w=0.0, current_a=0.0, energy_kwh=0.0),
        _channel(5, hidden=True),
    ]
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy,
        [_meter(channels=channels)],
    )

    expect(page.locator("#circuitsList .circuit-row")).to_have_count(2)
    toggle = page.locator("#circuitsHiddenToggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_text("Show hidden (1)")

    toggle.click()
    rows = page.locator("#circuitsList .circuit-row")
    expect(rows).to_have_count(3)
    expect(rows.nth(2)).to_contain_text("Clamp 5")
    expect(rows.nth(2).locator(".device-hidden-chip")).to_have_text("Hidden")
    expect(rows.nth(2)).to_have_class(re.compile(r"\bis-hidden-circuit\b"))
    expect(toggle).to_have_text("Hide hidden")

    # The revealed row's dialog shows the flag it was put away with.
    rows.nth(2).locator(".action-row-main").click()
    expect(page.locator("#circuitHiddenToggle")).to_have_attribute("aria-checked", "true")


def test_the_hidden_toggle_stays_out_of_the_way_when_nothing_is_hidden(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """Nothing put away, nothing to offer to bring back."""
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])
    expect(page.locator("#circuitsHiddenToggle")).to_be_hidden()


def test_the_hidden_toggle_filters_rows_from_the_group_footer(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """It sits in the group's footer, never its header (#779, #884).

    A control in the header is an ambiguous tap (design.md, LAYOUT-05). The
    footer verb only filters the list, in both directions.
    """
    channels = [
        _channel(1, display_name="water heater", power_w=291.5),
        _channel(2, hidden=True),
    ]
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy,
        [_meter(channels=channels)],
    )

    toggle = page.locator("#circuitsHiddenToggle")
    # In the group's footer, not in the header.
    expect(page.locator("#circuitsCard .group-head #circuitsHiddenToggle")).to_have_count(0)
    expect(page.locator("#circuitsCard .group-foot #circuitsHiddenToggle")).to_have_count(1)
    expect(page.locator("#circuitsList .circuit-row")).to_have_count(1)

    toggle.click()
    expect(page.locator("#circuitsList .circuit-row")).to_have_count(2)

    toggle.click()
    expect(page.locator("#circuitsList .circuit-row")).to_have_count(1)
