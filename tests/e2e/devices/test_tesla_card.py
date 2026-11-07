"""The car card on Devices: figures, hidden when not set up, empty and stale.

`#teslaCard` reads ``GET /api/tesla`` (the local store; the route never calls
Tesla). The figures test runs at phone and desktop widths in light and dark;
the rest are DOM-state checks and run on Chromium only.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import assert_no_horizontal_overflow

_THEME_KEY = "home-automation.theme"
_EXPECTED_BG = {"light": "rgb(255, 255, 255)", "dark": "rgb(13, 17, 23)"}
_LEGS = [(390, "light"), (390, "dark"), (1280, "light"), (1280, "dark")]


def _open_devices(page: Page, base_url: str) -> None:
    boot_home(page, base_url)
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")


@pytest.mark.parametrize("leg", _LEGS, ids=lambda leg: f"{leg[0]}px-{leg[1]}")
def test_car_card_shows_battery_range_charging_and_last_seen(
    leg, page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_tesla: Callable, sample_tesla: Dict,
) -> None:
    width, theme = leg
    mock_api(sample_units)
    mock_energy()
    mock_tesla(sample_tesla)
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem({json.dumps(_THEME_KEY)}, {json.dumps(theme)})")
    _open_devices(page, base_url)

    assert page.evaluate("document.documentElement.dataset.theme") == theme
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == _EXPECTED_BG[theme]

    card = page.locator("#teslaCard")
    expect(card).to_be_visible()
    expect(card).to_have_attribute("data-state", "ready")
    expect(page.get_by_test_id("tesla-battery")).to_have_text("72 %")
    expect(page.get_by_test_id("tesla-range")).to_have_text("352 km range")
    expect(page.locator("#teslaMeter")).to_have_attribute("aria-valuenow", "72")
    expect(page.get_by_test_id("tesla-charging")).to_have_text("Charging")
    expect(page.locator("#teslaChargingMeta")).to_have_text("7.2 kW · +12.4 kWh added")
    expect(page.get_by_test_id("tesla-seen")).to_have_text(re.compile(r"^Online · data from \d+ min ago$"))
    expect(page.locator("#teslaSeenAvatar")).to_have_attribute("data-badge", "up")
    # Read-only: the card carries no control at all.
    expect(card.locator("button, input, [role=switch]")).to_have_count(0)
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
def test_car_card_hidden_when_not_configured(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    with page.expect_response("**/api/tesla"):
        _open_devices(page, base_url)
    expect(page.locator("#teslaCard")).to_be_hidden()


@pytest.mark.chromium_only
def test_car_card_empty_state_while_collection_is_off(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_tesla: Callable, sample_tesla: Dict,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tesla({**sample_tesla, "sampler_enabled": False, "sample": None, "state": None})
    _open_devices(page, base_url)

    card = page.locator("#teslaCard")
    expect(card).to_have_attribute("data-state", "empty")
    expect(card.locator(".empty-state-message")).to_have_text("Car data collection is off")
    expect(page.locator("#teslaRows")).to_be_hidden()


@pytest.mark.chromium_only
def test_car_card_asleep_then_stale_keeps_the_figures(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_tesla: Callable, sample_tesla: Dict,
) -> None:
    mock_api(sample_units)
    mock_energy()
    asleep = {**sample_tesla, "state": "asleep",
              "sample": {**sample_tesla["sample"], "charging_state": "Complete", "charger_power_kw": 0}}
    mock_tesla(asleep)
    _open_devices(page, base_url)

    expect(page.get_by_test_id("tesla-charging")).to_have_text("Charge complete")
    expect(page.locator("#teslaChargingMeta")).to_have_text("+12.4 kWh added")
    expect(page.get_by_test_id("tesla-seen")).to_have_text(re.compile(r"^Asleep · "))
    expect(page.locator("#teslaSeenAvatar")).not_to_have_attribute("data-badge", re.compile(".*"))

    page.route("**/api/tesla", lambda route: route.fulfill(status=503, body='{"detail":"down"}'))
    page.locator("#tabHome").click()
    page.locator("#tabIot").click()

    card = page.locator("#teslaCard")
    expect(card).to_have_attribute("data-state", "stale")
    expect(page.locator("#teslaState")).to_contain_text("live data unavailable")
    expect(page.get_by_test_id("tesla-battery")).to_have_text("72 %")
