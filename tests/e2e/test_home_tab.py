"""Home tab surfaces — the weather tile and the read-only AC summary line.

The Home pane's other cards have their own modules: `test_vm_tile.py` (the
Home Assistant VM card) and `test_home_assistant.py` (its voice satellites).
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import (
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    effective_rects,
)


def test_weather_icon_controls_have_non_overlapping_44px_targets(
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

    buttons = page.locator(".weather-icon-btn")
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
