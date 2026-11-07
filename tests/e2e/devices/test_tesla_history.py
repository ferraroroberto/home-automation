"""The car's history sheet: figures, charts and recent sessions from the store.

``#teslaHistorySheet`` opens from the Car card's History link and reads
``GET /api/tesla/history`` and ``/sessions`` (the local store; neither calls
Tesla). The figures test runs at phone and desktop widths in light and dark;
the rest are DOM-state checks and run on Chromium only.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import assert_no_horizontal_overflow

_THEME_KEY = "home-automation.theme"
_EXPECTED_BG = {"light": "rgb(255, 255, 255)", "dark": "rgb(13, 17, 23)"}
_LEGS = [(390, "light"), (390, "dark"), (1280, "light"), (1280, "dark")]


def _chart_data(page: Page, canvas_id: str) -> List[List[float]]:
    return page.evaluate(
        "(id) => Chart.getChart(document.getElementById(id)).data.datasets.map((d) => d.data)",
        canvas_id,
    )


def _open_history(page: Page, base_url: str) -> None:
    boot_home(page, base_url)
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.locator("#teslaHistoryOpen").click()
    expect(page.get_by_test_id("tesla-history-sheet")).to_be_visible()


@pytest.fixture
def car_set_up(mock_api: Callable, sample_units: List[Dict], mock_energy: Callable,
               mock_tesla: Callable, sample_tesla: Dict) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tesla(sample_tesla)


@pytest.mark.parametrize("leg", _LEGS, ids=lambda leg: f"{leg[0]}px-{leg[1]}")
def test_history_sheet_shows_figures_charts_and_sessions(
    leg, page: Page, base_url: str, car_set_up: None, mock_tesla_history: Callable,
    sample_tesla_history: Callable, sample_tesla_sessions: Dict,
) -> None:
    width, theme = leg
    mock_tesla_history(sample_tesla_history, sample_tesla_sessions)
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem({json.dumps(_THEME_KEY)}, {json.dumps(theme)})")
    _open_history(page, base_url)

    assert page.evaluate("document.documentElement.dataset.theme") == theme
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == _EXPECTED_BG[theme]

    body = page.locator("#teslaHistoryBody")
    expect(body).to_have_attribute("data-state", "ready")
    expect(page.locator("#teslaStatKm")).to_have_text("214 km")
    expect(page.locator("#teslaStatCharged")).to_have_text("38.4 kWh")
    expect(page.locator("#teslaStatEff")).to_have_text("16.0 kWh/100 km")
    expect(page.locator("#teslaStatsCaption")).to_have_text(
        "This week so far · 6 drives · 2 charging sessions · 30.2 kWh at home"
    )

    # Twelve weekly bars ending this week; the stubbed three sit at the end.
    page.wait_for_function("() => window.Chart && Chart.getChart(document.getElementById('teslaChargeChart'))")
    [km] = _chart_data(page, "teslaKmChart")
    assert len(km) == 12
    assert km[-3:] == [120.0, 310.5, 214.0] and not any(km[:-3])
    home, away = _chart_data(page, "teslaChargeChart")
    assert home[-2:] == [40.1, 30.2]
    assert [round(v, 1) for v in away[-2:]] == [12.2, 8.2]

    charges = page.get_by_test_id("tesla-charge-list").locator(".action-row-title")
    expect(charges).to_have_text(["9.1 kWh · Home", "29.3 kWh · Away"])
    expect(page.get_by_test_id("tesla-drive-list").locator(".action-row-title")).to_have_text(["25.0 km"])
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
def test_month_toggle_reads_the_monthly_figures(
    page: Page, base_url: str, car_set_up: None, mock_tesla_history: Callable,
    sample_tesla_history: Callable, sample_tesla_sessions: Dict,
) -> None:
    mock_tesla_history(sample_tesla_history, sample_tesla_sessions)
    _open_history(page, base_url)
    expect(page.locator("#teslaHistoryBody")).to_have_attribute("data-state", "ready")

    with page.expect_request("**/api/tesla/history?period=month"):
        page.locator("#teslaHistoryPeriod [data-period=month]").click()
    expect(page.locator("#teslaHistoryPeriod [data-period=month]")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#teslaStatsCaption")).to_contain_text("This month so far")
    page.wait_for_function(
        "() => window.Chart && Chart.getChart(document.getElementById('teslaKmChart'))?.data.labels.length === 12"
    )


@pytest.mark.chromium_only
def test_history_empty_state_while_collection_is_off(
    page: Page, base_url: str, car_set_up: None, mock_tesla_history: Callable,
) -> None:
    def _empty(period: str) -> Dict:
        return {"configured": True, "sampler_enabled": False, "period": period, "buckets": []}

    mock_tesla_history(_empty, {"configured": True, "drives": [], "charging": []})
    _open_history(page, base_url)

    body = page.locator("#teslaHistoryBody")
    expect(body).to_have_attribute("data-state", "empty")
    expect(body.locator(".empty-state-message")).to_have_text("Car data collection is off")
    expect(page.locator("#teslaHistoryData")).to_be_hidden()


@pytest.mark.chromium_only
def test_history_error_offers_retry(page: Page, base_url: str, car_set_up: None) -> None:
    page.route("**/api/tesla/history*", lambda route: route.fulfill(status=503, body='{"detail":"down"}'))
    _open_history(page, base_url)

    body = page.locator("#teslaHistoryBody")
    expect(body).to_have_attribute("data-state", "error")
    expect(body.locator(".empty-state-action")).to_have_text("Retry")
