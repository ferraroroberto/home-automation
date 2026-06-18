"""Inline card controls — power, target stepper, fan — POST and re-render.

Each write hits POST /api/units/{id}; the stub echoes the merged snapshot
and only that card re-renders from the response.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector(".unit-card", state="attached")


def test_power_toggle_posts_and_rerenders(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    off = page.locator('[data-unit-id="unit-2"]')  # starts OFF
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-2") and r.method == "POST"
    ) as info:
        off.locator(".toggle").click()
    assert info.value.post_data_json == {"power": True}
    # Card re-renders ON from the read-back.
    expect(off.locator(".toggle")).to_have_attribute("aria-checked", "true")


def test_target_stepper_posts_set_temperature(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    card = page.locator('[data-unit-id="unit-1"]')  # set 24.0, step 0.5
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        card.locator(".stepper .plus").click()
    assert info.value.post_data_json == {"set_temperature": 24.5}
    expect(card.locator(".target-value")).to_contain_text("24.5")


def test_fan_change_posts_fan_speed(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    card = page.locator('[data-unit-id="unit-1"]')
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        card.locator(".unit-fan select").select_option("Three")
    assert info.value.post_data_json == {"fan_speed": "Three"}
    expect(card.locator(".unit-fan select")).to_have_value("Three")


def test_target_clamped_at_min(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """A unit at its Cool minimum (16) shouldn't POST a below-range value."""
    sample_units[0]["set_temperature"] = 16.0
    mock_api(sample_units)
    _boot(page, base_url)
    card = page.locator('[data-unit-id="unit-1"]')
    posted = {"hit": False}
    page.on("request", lambda r: posted.update(hit=True)
            if (r.method == "POST" and r.url.endswith("/api/units/unit-1")) else None)
    card.locator(".stepper .minus").click()
    page.wait_for_timeout(300)
    assert posted["hit"] is False, "minus at the floor must not POST a sub-range value"
    expect(card.locator(".target-value")).to_contain_text("16.0")
