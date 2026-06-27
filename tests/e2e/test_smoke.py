"""Smoke tests — the dashboard boots and renders the unit cards.

Tight by design: catches JS exceptions on boot, a broken render, and the
core card anatomy (power toggle, target stepper, room readout). Expand
only when a real regression slips through.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
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
    page.wait_for_selector(".unit-card", state="attached")
    page.wait_for_timeout(300)
    assert errors == [], "JS errors during boot:\n  - " + "\n  - ".join(errors)


def test_renders_all_unit_cards(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    cards = page.locator(".unit-card")
    expect(cards).to_have_count(len(sample_units))
    # Names from the fixtures land in the headers.
    for u in sample_units:
        card = page.locator(f'[data-unit-id="{u["unit_id"]}"]')
        expect(card.locator(".unit-name")).to_have_text(u["name"])


def test_card_anatomy(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    card = page.locator('[data-unit-id="unit-1"]')
    # Power toggle reflects power=True.
    toggle = card.locator(".toggle")
    expect(toggle).to_have_attribute("aria-checked", "true")
    # Room readout + target value present.
    expect(card.locator(".unit-room .value")).to_contain_text("22.5")
    expect(card.locator(".target-value")).to_contain_text("24.0")
    # Fan select rendered with the current value (.unit-fan is the <select>).
    expect(card.locator("select.unit-fan")).to_have_value("Auto")


def test_off_unit_marked(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    off = page.locator('[data-unit-id="unit-2"]')
    expect(off).to_have_class("card unit-card is-off")
    expect(off.locator(".toggle")).to_have_attribute("aria-checked", "false")


def test_detail_modal_adds_multiple_schedule_entries(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    page.locator("#tabAc").click()
    page.locator('[data-unit-id="unit-1"] .unit-header').click()
    page.locator("#detailScheduleSection > summary").click()

    add = page.get_by_role("button", name="+ Add schedule")
    add.click()
    add.click()

    entries = page.locator(".schedule-entry")
    expect(entries).to_have_count(2)
    entries.nth(1).locator(".sched-entry-power").select_option("false")
    expect(entries.nth(1).locator(".schedule-profile")).to_be_hidden()

    # Schedules now persist only on Save (#202); the card badge updates after.
    page.locator("#detailSave").click()
    badge = page.locator('[data-unit-id="unit-1"] .unit-schedule-badge')
    expect(badge).to_contain_text("2")
