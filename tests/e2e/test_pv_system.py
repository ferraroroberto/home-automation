"""Settings: the PV-system editor (on the Energy tab until #779) that feeds the solar forecast (issue #561).

The array config used to be file-only — these cover the browser half of making
it editable: the card renders the stored panel rows, the staged dialog rejects a
value rather than silently clamping it, and a saved row is reflected straight
back into the forecast card's params line (the only user-visible proof that the
forecast is now computed from what was just typed).

Also covers issue #564: the save toast carries the recomputed day estimate
(the forecast card sits above this one, off-screen on a phone when editing),
and degrades to the plain confirmation when no estimate is available.

Runs against stubbed energy endpoints (``mock_energy``), whose PV-system route
is stateful — no network, no real ``config/pv_system.json``.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect

from tests.e2e._app import open_settings_sheet


def _boot_pv_system(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, **energy_kwargs,
) -> None:
    mock_api(sample_units)
    mock_energy(**energy_kwargs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    # Visit Energy first: its entry loads the forecast card whose params line
    # the editor's saves are checked against. The editor itself moved off the
    # Energy tab into Settings in #779.
    page.locator("#tabEnergy").click()
    page.wait_for_selector("#paneEnergy", state="visible")
    open_settings_sheet(page, "pvSystemSheet")  # a Settings sheet since #886


def test_card_renders_a_summary_row_per_panel_row(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[
            {"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0},
            {"kwp": 0.9, "tilt_deg": 15.0, "azimuth_deg": 180.0},
        ],
    )

    rows = page.locator("#pvArrayList .automation-summary-row")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("7.9 kWp · 15° · S")
    expect(rows.first).to_contain_text("facing south")
    expect(rows.nth(1)).to_contain_text("0.9 kWp · 15° · N")
    # The header carries the system total so the card reads without expanding.
    expect(page.locator("#pvSystemTotal")).to_have_text("8.8 kWp")


def test_empty_config_shows_the_empty_state_not_a_blank_list(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(page, base_url, sample_units, mock_api, mock_energy, pv_arrays=[])

    expect(page.locator("#pvArrayList .empty-state")).to_be_visible()
    expect(page.locator("#pvArrayList")).to_contain_text("No panel rows yet")
    expect(page.locator("#pvArrayAdd")).to_be_visible()


def test_adding_a_row_validates_then_updates_the_forecast_and_its_toast(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """One editor session: an invalid tilt is refused against its field, the
    corrected row saves and reaches the forecast, and a later save with no
    estimate available degrades the toast instead of reading undefined/NaN."""
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )
    rows = page.locator("#pvArrayList .automation-summary-row")
    dialog = page.locator("#pvArrayDialog")
    toast = page.locator("#toast")
    expect(page.locator("#forecastParams")).to_have_text("7.9 kWp · 15° · S · PR 0.80")

    # A negative tilt is the mistake the azimuth convention invites, so it
    # must explain itself rather than be silently clamped to 0.
    page.locator("#pvArrayAdd").click()
    expect(dialog).to_be_visible()
    page.locator("#pvArrayKwp").fill("1")
    page.locator("#pvArrayTilt").fill("-15")
    page.locator("#pvArraySave").click()

    error = page.locator("#pvArrayTiltError")
    expect(error).to_be_visible()
    expect(error).to_contain_text("between 0 and 90")
    expect(page.locator("#pvArrayTilt")).to_have_attribute("aria-invalid", "true")
    # Still open, still one row — nothing was persisted.
    expect(dialog).to_be_visible()
    expect(rows).to_have_count(1)

    page.locator("#pvArrayKwp").fill("0.9")
    page.locator("#pvArrayTilt").fill("15")
    page.locator("#pvArrayAzimuth").fill("180")
    # The convention hint echoes what was typed, in words.
    expect(page.locator("#pvArrayAzimuthEcho")).to_have_text("facing north")
    page.locator("#pvArraySave").click()

    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(2)
    # The whole point: the forecast is now computed from the edited array.
    expect(page.locator("#forecastParams")).to_have_text(
        "7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80"
    )
    # Issue #564: the save toast carries that same recomputed estimate rather
    # than firing before the forecast refetch lands (mock_energy's forecast
    # fixture fixes expected_total_kwh at 12.3, so this is deterministic).
    expect(toast).to_have_text("PV system saved · today's estimate 12.3 kWh")

    # Issue #564: a forecast that comes back unavailable just drops the suffix.
    page.route(
        "**/api/energy/forecast*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": false, "reason": "no_config"}',
        ),
    )
    page.locator("#pvArrayAdd").click()
    page.locator("#pvArrayKwp").fill("0.9")
    page.locator("#pvArrayTilt").fill("15")
    page.locator("#pvArrayAzimuth").fill("180")
    page.locator("#pvArraySave").click()

    expect(dialog).to_be_hidden()
    expect(toast).to_have_text("PV system saved")
