"""Energy tab: the fleet solar-boost sequencing card (issue #562).

The knobs deciding how A/C units enter and leave solar boost used to be `.env`
-only and needed a tray restart to change. These cover the browser half of making
them editable: the card reads without expanding, an edit persists, and the
5-minute floor is refused with an explanation rather than silently clamped.

Runs against the stubbed energy/HVAC endpoints (``mock_energy``), whose
coordinator route is stateful — no network, no real ``config/hvac_boost.json``.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot_boost_card(
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
    page.eval_on_selector("#boostCoordCard", "el => { el.open = true; }")


def test_card_summary_reads_without_expanding(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={"settle_interval_s": 600, "admission_margin_w": 250.0},
    )
    # The two knobs that decide how fast the fleet ramps, in the header.
    expect(page.locator("#boostCoordSummary")).to_have_text("10 min · +250 W")


def test_stored_values_populate_the_fields_with_the_floor_as_the_input_min(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={"settle_interval_s": 900, "hard_deficit_w": 1500.0},
    )
    # Seconds on the wire, minutes on screen — that conversion is the card's.
    expect(page.locator("#boostSettleMin")).to_have_value("15")
    expect(page.locator("#boostHardDeficit")).to_have_value("1500")
    # The floor is served, not hand-copied into the frontend.
    expect(page.locator("#boostSettleMin")).to_have_attribute("min", "5")


def test_editing_the_settle_interval_persists_and_updates_the_summary(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(page, base_url, sample_units, mock_api, mock_energy)
    expect(page.locator("#boostCoordSummary")).to_have_text("5 min · +0 W")

    page.locator("#boostSettleMin").fill("12")
    page.locator("#boostSettleMin").press("Tab")

    expect(page.locator("#boostCoordSummary")).to_have_text("12 min · +0 W")
    # Reflected back from the PUT response, i.e. actually persisted.
    expect(page.locator("#boostSettleMin")).to_have_value("12")


def test_editing_the_admission_margin_persists(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(page, base_url, sample_units, mock_api, mock_energy)

    page.locator("#boostAdmissionMargin").fill("300")
    page.locator("#boostAdmissionMargin").press("Tab")

    expect(page.locator("#boostCoordSummary")).to_have_text("5 min · +300 W")


def test_a_settle_interval_under_the_floor_is_refused_and_rolled_back(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The floor is physical — the solar meter only publishes every 5 minutes —
    so a shorter interval must explain itself, not be silently clamped."""
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={"settle_interval_s": 600},
    )

    page.locator("#boostSettleMin").fill("2")
    page.locator("#boostSettleMin").press("Tab")

    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("at least 5 min")
    # Rolled back to what is stored — the card never shows an unsaved value.
    expect(page.locator("#boostSettleMin")).to_have_value("10")
    expect(page.locator("#boostCoordSummary")).to_have_text("10 min · +0 W")


def test_a_negative_margin_is_refused_and_rolled_back(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={"admission_margin_w": 100.0},
    )

    page.locator("#boostAdmissionMargin").fill("-50")
    page.locator("#boostAdmissionMargin").press("Tab")

    expect(page.locator("#toast")).to_contain_text("0 W or more")
    expect(page.locator("#boostAdmissionMargin")).to_have_value("100")
