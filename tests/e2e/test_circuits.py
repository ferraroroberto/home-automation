"""Circuits card (issue #25): every CT-clamp channel renders, clamp or not.

Drives the IoT tab's Circuits card against a stubbed ``GET /api/circuits`` (no
mDNS, no meter I/O) on both the Chromium-desktop and WebKit/iPhone projections.

The contract worth a browser test is the one a well-meaning refactor would
quietly break: **a channel reading 0 W is never filtered out**. More clamps get
fitted over time, and a channel that vanishes because it currently measures
nothing is indistinguishable, on screen, from a channel that was never there.
Issue #619 added a *user*-driven hide on top of that, which makes the
distinction sharper rather than softer: hidden is a decision, 0 W never is.

The rest of #619's card shape is here too, because it is all state the DOM
holds rather than the server — the fold state has to survive a re-render (the
card repaints every poll), and the group order is computed client-side.
"""

from __future__ import annotations

import json
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
    """One dialog serves both; a meter has no clamp direction to correct.

    Nor a hidden flag — hiding a meter would hide every circuit under it, which
    is what folding the group is for (issue #619).
    """
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    page.locator("#circuitsList .circuit-row .device-row-name").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("water heater")
    expect(page.locator("#circuitInvertSection")).to_be_visible()
    expect(page.locator("#circuitInvertToggle")).to_have_attribute("aria-checked", "true")
    expect(page.locator("#circuitHiddenSection")).to_be_visible()
    page.locator("#circuitDetailClose").click()

    page.locator("#circuitsList .circuit-meter-name").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(page.locator("#circuitDetailName")).to_have_text("Athom Energy Monitor ddee01")
    expect(page.locator("#circuitInvertSection")).to_be_hidden()
    expect(page.locator("#circuitHiddenSection")).to_be_hidden()


def test_the_meter_header_carries_the_name_alone(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#619: the group's aggregate left the header for the dialog.

    This card answers "where is the power going", so the meter's own total,
    mains voltage and Wi-Fi signal are reference figures — not the one thing
    the header should be spending its width on.
    """
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    header = page.locator("#circuitsList .circuit-meter").first
    expect(header).to_contain_text("Athom Energy Monitor ddee01")
    for reading in ("292 W", "239 V", "-68 dBm"):
        expect(header).not_to_contain_text(reading)

    # They are all still reachable, one tap away, plus the meter's MAC.
    page.locator("#circuitsList .circuit-meter-name").first.click()
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

    row.locator(".device-row-name").click()
    expect(page.locator("#circuitReadings")).to_be_visible()
    expect(page.locator("#circuitReadingPower")).to_have_text("292 W")
    expect(page.locator("#circuitReadingCurrent")).to_have_text("1.81 A")
    expect(page.locator("#circuitReadingEnergy")).to_have_text("6.88 kWh")
    # A meter has no per-clamp readings block of its own.
    page.locator("#circuitDetailClose").click()
    page.locator("#circuitsList .circuit-meter-name").first.click()
    expect(page.locator("#circuitReadings")).to_be_hidden()


def test_meter_groups_are_ordered_by_name_not_discovery(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A→Z on the visible label, numeric-aware (issue #619).

    mDNS hands meters back in whatever order the sweep saw them, so the only
    way to choose the order of the board is to rename the meters — which only
    works if "2 …" sorts before "10 …" rather than lexically after it.
    """
    meters = [
        _meter(meter_id="AA:BB:CC:DD:EE:10", display_name="10 garage"),
        _meter(meter_id="AA:BB:CC:DD:EE:02", display_name="2 kitchen"),
        _meter(meter_id="AA:BB:CC:DD:EE:01", display_name="1 cuadro principal"),
    ]
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, meters)

    names = page.locator("#circuitsList .circuit-meter-name")
    expect(names).to_have_count(3)
    expect(names.nth(0)).to_have_text("1 cuadro principal")
    expect(names.nth(1)).to_have_text("2 kitchen")
    expect(names.nth(2)).to_have_text("10 garage")


def test_a_meter_group_folds_and_the_name_still_renames(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The summary-embedded control: the name edits, it never folds."""
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])

    group = page.locator("#circuitsList .circuit-group").first
    body = page.locator("#circuitsList .circuit-group-body").first
    expect(group).to_have_js_property("open", True)
    expect(body).to_be_visible()

    page.locator("#circuitsList .circuit-meter .collapse-chevron").first.click()
    expect(group).to_have_js_property("open", False)
    expect(body).to_be_hidden()

    # Tapping the name of a folded group opens the dialog and leaves it folded.
    page.locator("#circuitsList .circuit-meter-name").first.click()
    expect(page.locator("#circuitDialog")).to_be_visible()
    expect(group).to_have_js_property("open", False)


def test_the_fold_survives_a_re_render_and_a_reload(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """renderCircuits() rebuilds the list every poll — the DOM cannot hold this.

    A fold that quietly springs open every 15 seconds is worse than no fold at
    all, so the collapsed set lives in localStorage and is re-applied on render.
    """
    channels = [
        _channel(1, display_name="water heater", power_w=291.5, current_a=1.81,
                 energy_kwh=6.88),
        _channel(2, hidden=True),
    ]
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy,
        [_meter(channels=channels)],
    )

    page.locator("#circuitsList .circuit-meter .collapse-chevron").first.click()
    expect(page.locator("#circuitsList .circuit-group").first).to_have_js_property(
        "open", False
    )

    # "Show hidden" re-renders the whole list — the same path the poll takes.
    page.locator("#circuitsHiddenToggle").click()
    expect(page.locator("#circuitsList .circuit-group").first).to_have_js_property(
        "open", False
    )

    page.reload(wait_until="domcontentloaded")
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )
    expect(page.locator("#circuitsList .circuit-group").first).to_have_js_property(
        "open", False
    )


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
    expect(rows.nth(2)).to_contain_text("hidden")
    expect(toggle).to_have_text("Hide hidden")

    # The revealed row's dialog shows the flag it was put away with.
    rows.nth(2).locator(".device-row-name").click()
    expect(page.locator("#circuitHiddenToggle")).to_have_attribute("aria-checked", "true")


def test_the_hidden_toggle_stays_out_of_the_way_when_nothing_is_hidden(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """Nothing put away, nothing to offer to bring back."""
    _boot_circuits(page, base_url, sample_units, mock_api, mock_energy, [_meter()])
    expect(page.locator("#circuitsHiddenToggle")).to_be_hidden()


def test_the_hidden_toggle_filters_without_folding_the_card(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """It rides in the card's own <summary>, next to the chevron.

    That is the whole trap of the summary-embedded-control pattern: a click
    lands on the disclosure unless it is explicitly stopped, so filtering the
    list would otherwise fold the card shut on the very same tap.
    """
    channels = [
        _channel(1, display_name="water heater", power_w=291.5),
        _channel(2, hidden=True),
    ]
    _boot_circuits(
        page, base_url, sample_units, mock_api, mock_energy,
        [_meter(channels=channels)],
    )

    card = page.locator("#circuitsCard")
    toggle = page.locator("#circuitsHiddenToggle")
    # In the header, not in a toolbar of its own.
    expect(page.locator("#circuitsCard > summary #circuitsHiddenToggle")).to_have_count(1)
    expect(card).to_have_js_property("open", True)

    toggle.click()
    expect(card).to_have_js_property("open", True)
    expect(page.locator("#circuitsList .circuit-row")).to_have_count(2)
