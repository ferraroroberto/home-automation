"""Energy tab: the PV-system editor that feeds the solar forecast (issue #561).

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


def _boot_pv_system(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, **energy_kwargs,
) -> None:
    mock_api(sample_units)
    mock_energy(**energy_kwargs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabEnergy").click()
    page.wait_for_selector("#paneEnergy", state="visible")
    # Collapsed by default, like every other settings card on the app.
    page.eval_on_selector("#pvSystemCard", "el => { el.open = true; }")


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


def test_adding_a_row_updates_the_forecast_params_line(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )
    expect(page.locator("#forecastParams")).to_have_text("7.9 kWp · 15° · S · PR 0.80")

    page.locator("#pvArrayAdd").click()
    expect(page.locator("#pvArrayDialog")).to_be_visible()
    page.locator("#pvArrayKwp").fill("0.9")
    page.locator("#pvArrayTilt").fill("15")
    page.locator("#pvArrayAzimuth").fill("180")
    # The convention hint echoes what was typed, in words.
    expect(page.locator("#pvArrayAzimuthEcho")).to_have_text("facing north")
    page.locator("#pvArraySave").click()

    expect(page.locator("#pvArrayDialog")).to_be_hidden()
    expect(page.locator("#pvArrayList .automation-summary-row")).to_have_count(2)
    # The whole point: the forecast is now computed from the edited array.
    expect(page.locator("#forecastParams")).to_have_text(
        "7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80"
    )
    # Issue #564: the save toast carries that same recomputed estimate rather
    # than firing before the forecast refetch lands (mock_energy's forecast
    # fixture fixes expected_total_kwh at 12.3, so this is deterministic).
    expect(page.locator("#toast")).to_have_text("PV system saved · today's estimate 12.3 kWh")


def test_toast_degrades_to_plain_text_when_no_estimate_is_available(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The save must never fire a toast reading undefined/NaN — a forecast that
    comes back unavailable just drops the suffix (issue #564)."""
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )
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

    expect(page.locator("#pvArrayDialog")).to_be_hidden()
    expect(page.locator("#toast")).to_have_text("PV system saved")


def test_an_invalid_tilt_is_reported_against_its_field_not_saved(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A negative tilt is the mistake the azimuth convention invites, so it must
    explain itself rather than be silently clamped to 0."""
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )

    page.locator("#pvArrayAdd").click()
    page.locator("#pvArrayKwp").fill("1")
    page.locator("#pvArrayTilt").fill("-15")
    page.locator("#pvArraySave").click()

    error = page.locator("#pvArrayTiltError")
    expect(error).to_be_visible()
    expect(error).to_contain_text("between 0 and 90")
    expect(page.locator("#pvArrayTilt")).to_have_attribute("aria-invalid", "true")
    # Still open, still one row — nothing was persisted.
    expect(page.locator("#pvArrayDialog")).to_be_visible()
    expect(page.locator("#pvArrayList .automation-summary-row")).to_have_count(1)
