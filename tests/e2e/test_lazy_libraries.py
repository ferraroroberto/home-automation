"""Chart.js and Leaflet load on first use, not at boot (#760).

A cold launch used to download and run both (~350 KB) before the boot
module for two tabs that may never open. The charts themselves are covered
where they render (`test_energy_tab.py`, `test_network.py`); this pins that
Home doesn't pay for them, and that the one Leaflet view still opens.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings_sheet

_LAZY_LIBRARIES = ("chart.umd.min.js", "leaflet.js", "leaflet.css")


def test_home_cold_load_requests_no_chart_or_map_library(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    requested: List[str] = []
    page.on("request", lambda req: requested.append(req.url))

    boot_home(page, base_url)
    page.wait_for_selector("#acUnits .ac-row", state="attached")
    page.wait_for_timeout(500)

    assert [u for u in requested if u.split("?")[0].endswith(_LAZY_LIBRARIES)] == []
    assert page.evaluate("typeof window.Chart") == "undefined"
    assert page.evaluate("typeof window.L") == "undefined"


def test_place_map_picker_loads_leaflet_on_first_open(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route("**/*.tile.openstreetmap.org/**", lambda route: route.abort())
    boot_home(page, base_url)

    open_settings_sheet(page, "presencePlacesSheet")  # Settings since #779
    page.locator("#presencePlaceAdd").click()
    page.locator("#presencePlacePickMap").click()

    expect(page.locator("#presenceMapPickerDialog")).to_be_visible()
    expect(page.locator("#presenceMapPicker.leaflet-container")).to_be_attached()
    assert page.evaluate("typeof window.L") == "object"
    # The stylesheet came with the script: Leaflet's CSS makes the container positioned.
    assert page.locator("#presenceMapPicker").evaluate("el => getComputedStyle(el).position") == "relative"
