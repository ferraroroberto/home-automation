"""Inline card controls — power, target stepper, fan — POST and re-render.

Each write hits POST /api/units/{id}; the stub echoes the merged snapshot
and only that card re-renders from the response.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

from playwright.sync_api import Locator, Page, expect

from tests.e2e._geometry import assert_no_horizontal_overflow


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Unit cards live in the AC tab now — activate it before interacting.
    page.locator("#tabAc").click()
    page.wait_for_selector(".unit-card", state="visible")


def _stable_bounding_box(locator: Locator) -> Optional[Dict[str, float]]:
    """Wait for visibility, then measure. Under full-suite load the card grid
    can re-render between the wait and the read, so retry once if the first
    read still lands mid-repaint (#431)."""
    expect(locator).to_be_visible()
    box = locator.bounding_box()
    if box is None:
        expect(locator).to_be_visible()
        box = locator.bounding_box()
    return box


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
        card.locator("select.unit-fan").select_option("Three")
    assert info.value.post_data_json == {"fan_speed": "Three"}
    expect(card.locator("select.unit-fan")).to_have_value("Three")


def test_offline_unit_card_is_dimmed_and_inert(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """An unreachable unit dims, says why, and cannot be commanded (#520).

    The bug this guards: controls that look live but silently no-op because the
    unit lost its cloud connection.
    """
    sample_units[1]["reachable"] = False  # unit-2 (Studio)
    mock_api(sample_units)
    _boot(page, base_url)

    offline = page.locator('[data-unit-id="unit-2"]')
    expect(offline).to_have_class(re.compile(r"\bis-unavailable\b"))
    expect(offline.locator(".unit-offline-badge")).to_have_text("Offline")
    # Every command-sending control is inert; readings stay on screen.
    expect(offline.locator(".toggle")).to_be_disabled()
    expect(offline.locator("select.unit-fan")).to_be_disabled()
    expect(offline.locator(".stepper .minus")).to_be_disabled()
    expect(offline.locator(".stepper .plus")).to_be_disabled()
    expect(offline.locator(".unit-room .value")).to_contain_text("19.0")

    # A reachable sibling is untouched.
    online = page.locator('[data-unit-id="unit-1"]')
    expect(online).not_to_have_class(re.compile(r"\bis-unavailable\b"))
    expect(online.locator(".unit-offline-badge")).to_have_count(0)
    expect(online.locator(".toggle")).to_be_enabled()


def test_offline_unit_row_marked_on_home_summary(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The Home tile mirrors the AC tab's offline state (#520)."""
    sample_units[1]["reachable"] = False  # unit-2 (Studio)
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#acSummary .ac-line", state="visible")

    row = page.locator("#acSummary .ac-line", has_text="Studio")
    expect(row).to_have_class(re.compile(r"\bis-unavailable\b"))
    expect(row.locator(".ac-line-offline")).to_have_text("Offline")
    expect(row.locator(".ac-line-toggle")).to_be_disabled()
    # Only the offline unit is marked.
    expect(page.locator("#acSummary .ac-line-offline")).to_have_count(1)
    expect(page.locator("#acSummary .ac-line-toggle:enabled")).to_have_count(
        len(sample_units) - 1
    )


def test_unit_header_has_44px_target_without_overlapping_controls(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    _boot(page, base_url)
    card = page.locator('[data-unit-id="unit-1"]')
    header = _stable_bounding_box(card.locator(".unit-header"))
    fan = _stable_bounding_box(card.locator(".unit-fan-control"))
    power = _stable_bounding_box(card.locator(".toggle"))
    assert header is not None and fan is not None and power is not None
    assert header["height"] >= 44
    assert header["x"] + header["width"] <= fan["x"]
    assert header["x"] + header["width"] <= power["x"]
    assert_no_horizontal_overflow(page)


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
