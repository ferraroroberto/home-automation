"""Per-unit detail modal — mode + vanes, with vane rows gated on caps."""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _open_detail(page: Page, base_url: str, unit_id: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Unit cards live in the AC tab now — activate it before opening a modal.
    page.locator("#tabAc").click()
    page.wait_for_selector(".unit-card", state="visible")
    page.locator(f'[data-unit-id="{unit_id}"] .unit-header').click()
    expect(page.locator("#detailDialog")).to_be_visible()


def test_modal_shows_mode_and_both_vanes(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")  # has both vanes
    expect(page.locator("#detailName")).to_have_text("Office")
    expect(page.locator("#detailMode")).to_have_value("Cool")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_visible()


def test_vane_rows_gated_on_capability(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    # unit-2: vertical only.
    _open_detail(page, base_url, "unit-2")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_hidden()


def test_no_vane_unit_hides_both(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-3")  # no vanes
    expect(page.locator("#detailVaneVerticalRow")).to_be_hidden()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_hidden()


def test_vane_change_posts_direction(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator("#detailVaneVertical").select_option("Swing")
    assert info.value.post_data_json == {"vane_vertical_direction": "Swing"}


def test_mode_change_posts(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator("#detailMode").select_option("Heat")
    assert info.value.post_data_json == {"operation_mode": "Heat"}
