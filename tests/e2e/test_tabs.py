"""Tab navigation + the new Home summary and Energy dashboard.

Covers the Home / AC / Energy switcher, the read-only AC summary on Home, and
an Energy-tab render (the live flow row + charts) against stubbed energy
fixtures — on both the Chromium-desktop and WebKit/iPhone projections.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


def test_tab_navigation_switches_panes(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    _boot(page, base_url)

    # Default: Home visible, the other panes hidden.
    expect(page.locator("#paneHome")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy")).to_be_hidden()

    # Home now shows the same Solar → Home ← Grid flow card as Energy (issue #57),
    # revealed once the first /api/energy read lands.
    expect(page.locator("#paneHome .flow-row")).to_be_visible()
    expect(page.locator("#homeFlowPv")).to_have_text("2,500 W")

    # AC tab → unit cards become visible.
    page.locator("#tabAc").click()
    expect(page.locator("#paneAc")).to_be_visible()
    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator(".unit-card").first).to_be_visible()

    # Energy tab → the live flow row shows.
    page.locator("#tabEnergy").click()
    expect(page.locator("#paneEnergy")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy .flow-row")).to_be_visible()


def test_home_shows_ac_summary_line_per_unit(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    _boot(page, base_url)

    lines = page.locator("#acSummary .ac-line")
    expect(lines).to_have_count(len(sample_units))
    # One scannable line per unit: name + an actionable power toggle (issue #72).
    expect(page.locator("#acSummary")).to_contain_text("Office")
    expect(page.locator("#acSummary .ac-line-toggle")).to_have_count(len(sample_units))


def test_energy_tab_renders_flow_and_charts(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()  # default fixture: PV 2500 W, house 1300 W, export 1200 W
    _boot(page, base_url)
    page.locator("#tabEnergy").click()

    # Flow row: Solar / Home / Grid live values, with thousands separators.
    expect(page.locator("#flowPv")).to_have_text("2,500 W")
    expect(page.locator("#flowHouse")).to_have_text("1,300 W")
    expect(page.locator("#flowGrid")).to_have_text("1,200 W")
    # Exporting (surplus > 0) → the grid arrow points out (▶) and reads as export.
    grid_arrow = page.locator("#wireGrid")
    expect(grid_arrow).to_have_class("flow-arrow is-export")
    expect(grid_arrow).to_have_text("▶")

    # Today's generation card is populated from /api/energy/today.
    expect(page.locator("#genTotal")).to_have_text("9.00 kWh")
    # Savings €: now the tiered avoided-cost from /api/energy/cost?range=day
    # (the cost fixture's totals.savings), not the old flat-rate computation.
    expect(page.locator("#savEur")).to_have_text("€0.37")

    # Both chart canvases render once the pane is shown.
    expect(page.locator("#liveChart")).to_be_visible()
    expect(page.locator("#aggChart")).to_be_visible()

    # Cost & savings breakdown table: a row per tariff period + a Total row,
    # fed by the /api/energy/cost stub.
    expect(page.locator("#costBody tr")).to_have_count(3)
    expect(page.locator("#costFoot")).to_contain_text("Total")
    expect(page.locator("#costFoot")).to_contain_text("€0.37")


def test_security_tab_renders_presence_spike(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)

    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_be_visible()
    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator(".presence-row")).to_have_count(3)
    expect(page.locator(".presence-row.is-home")).to_contain_text("Home Phone")
    expect(page.locator(".presence-row.is-away")).to_contain_text("Away Phone")
    expect(page.locator(".presence-row.is-unknown")).to_contain_text("Keys")
