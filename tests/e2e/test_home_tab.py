"""Home tab surfaces — the page header, the weather tile and the read-only AC
summary line.

The Home pane's other cards have their own modules: `test_vm_tile.py` (the
Home Assistant VM card) and `test_home_assistant.py` (its voice satellites).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import (
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    effective_rects,
)


def test_home_header_controls_have_non_overlapping_44px_targets(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/weather",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "available": True,
                "label": "Home",
                "weather_code": 0,
                "is_day": True,
                "temperature_c": 24,
                "forecast_code": 1,
                "temp_min_c": 18,
                "temp_max_c": 27,
            }),
        ),
    )
    boot_home(page, base_url)
    expect(page.locator("#weatherTile")).to_be_visible()

    # The theme toggle + Settings gear moved from the weather tile into the
    # page header in #779; they keep the compact 34px + .hit-target recipe.
    buttons = page.locator("#paneHome .page-head .home-toggle")
    targets = effective_rects(buttons)
    assert len(targets) == 2
    for target in targets:
        assert (target.visual.width, target.visual.height) == (34, 34)
    assert_min_target(buttons)
    assert_no_overlap(buttons)
    # The two compact controls sit left-to-right with no shared tap zone.
    assert targets[0].effective.right <= targets[1].effective.left
    assert_no_horizontal_overflow(page)


def test_home_shows_ac_summary_line_per_unit(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    lines = page.locator("#acSummary .ac-line")
    expect(lines).to_have_count(len(sample_units))
    # One scannable line per unit: name + an actionable power toggle (issue #72).
    expect(page.locator("#acSummary")).to_contain_text("Office")
    expect(page.locator("#acSummary .ac-line-toggle")).to_have_count(len(sample_units))


def test_perf_review_ready_selector_is_visible_on_home(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """/perf-review scores a cold launch as ready when `.fleet.toml`'s selector
    shows; one that never shows on Home leaves the check unmeasured (#777)."""
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    selector = _perf_review_ready_selector()
    expect(page.locator(selector).first).to_be_visible()


def _perf_review_ready_selector() -> str:
    fleet = Path(__file__).resolve().parents[2] / ".fleet.toml"
    return tomllib.loads(fleet.read_text(encoding="utf-8"))["perf"]["review"]["ready_selector"]
