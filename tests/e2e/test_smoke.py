"""Smoke tests — the dashboard boots and renders the unit rows.

Tight by design: catches JS exceptions on boot, a broken render, and the
core row anatomy (power switch, avatar badge, room → set line). Expand
only when a real regression slips through.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot(page: Page, base_url: str) -> list:
    errors: list = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    return errors


def test_boots_without_console_errors(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    errors = _boot(page, base_url)
    page.wait_for_selector("#acUnits .ac-row", state="attached")
    page.wait_for_timeout(300)
    assert errors == [], "JS errors during boot:\n  - " + "\n  - ".join(errors)


def test_renders_unit_rows_with_their_anatomy(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    rows = page.locator("#acUnits .ac-row")
    expect(rows).to_have_count(len(sample_units))
    # Names from the fixtures land in the row titles.
    for u in sample_units:
        row = page.locator(f'#acUnits [data-unit-id="{u["unit_id"]}"]')
        expect(row.locator(".action-row-title")).to_have_text(u["name"])

    row = page.locator('#acUnits [data-unit-id="unit-1"]')
    # Power switch reflects power=True, and the running unit's avatar is badged.
    expect(row.locator(".ac-line-toggle")).to_have_attribute("aria-checked", "true")
    expect(row.locator(".row-avatar")).to_have_attribute("data-badge", "up")
    # One meta line: room → set.
    expect(row.locator(".action-row-meta")).to_contain_text("22.5")
    expect(row.locator(".action-row-meta")).to_contain_text("24.0")

    # An off unit: switch off, no badge.
    off = page.locator('#acUnits [data-unit-id="unit-2"]')
    expect(off.locator(".ac-line-toggle")).to_have_attribute("aria-checked", "false")
    expect(off.locator(".row-avatar")).not_to_have_attribute("data-badge", "up")
