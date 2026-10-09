"""AC controls — the row's power switch and the unit sheet's setpoint and fan.

Each write hits POST /api/units/{id}; the stub echoes the merged snapshot and
the rows re-render from the response. Since #881 the AC tab draws a unit on
the shared row (power is its switch) and the setpoint and fan live in the
unit sheet, which sends each change as it is made.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._geometry import assert_no_horizontal_overflow, effective_rect


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabAc").click()
    page.wait_for_selector("#acUnits .ac-row", state="visible")


def _row(page: Page, unit_id: str):
    return page.locator(f'#acUnits [data-unit-id="{unit_id}"]')


def _open_sheet(page: Page, base_url: str, unit_id: str) -> None:
    _boot(page, base_url)
    _row(page, unit_id).locator(".action-row-main").click()
    expect(page.locator("#detailDialog")).to_be_visible()


def _unit_posts(page: Page, unit_id: str) -> List[Dict]:
    posts: List[Dict] = []
    page.on("request", lambda r: posts.append(r.post_data_json)
            if (r.method == "POST" and r.url.endswith(f"/api/units/{unit_id}")) else None)
    return posts


def test_power_toggle_posts_and_rerenders(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-2") and r.method == "POST"
    ) as info:
        _row(page, "unit-2").locator(".ac-line-toggle").click()  # starts OFF
    assert info.value.post_data_json == {"power": True}
    # The row re-renders ON from the read-back, its avatar badged as running.
    expect(_row(page, "unit-2").locator(".ac-line-toggle")).to_have_attribute("aria-checked", "true")
    expect(_row(page, "unit-2").locator(".row-avatar")).to_have_attribute("data-badge", "up")


def test_setpoint_taps_settle_into_one_post(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """Decision 4 of #872: the setpoint lives in the sheet and is sent as it
    changes; a burst of taps settles into one write, not one per tap."""
    mock_api(sample_units)
    posts = _unit_posts(page, "unit-1")
    _open_sheet(page, base_url, "unit-1")  # set 24.0, step 0.5
    expect(page.locator("#detailSetTemp")).to_have_text("24.0°")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator("#detailTempUp").click()
        page.locator("#detailTempUp").click()
        expect(page.locator("#detailSetTemp")).to_have_text("25.0°")
    assert info.value.post_data_json == {"set_temperature": 25.0}
    # The row follows the read-back, and no second write trails the first.
    expect(_row(page, "unit-1").locator(".action-row-meta")).to_contain_text("25.0")
    page.wait_for_timeout(900)
    assert posts == [{"set_temperature": 25.0}]


def test_closing_the_sheet_sends_a_dialled_setpoint(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_sheet(page, base_url, "unit-1")
    page.locator("#detailTempDown").click()
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator("#detailDone").click()
    assert info.value.post_data_json == {"set_temperature": 23.5}
    expect(page.locator("#detailDialog")).to_be_hidden()


def test_setpoint_stops_at_the_mode_minimum(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """A unit at its Cool minimum (16) can't be dialled below it."""
    sample_units[0]["set_temperature"] = 16.0
    mock_api(sample_units)
    posts = _unit_posts(page, "unit-1")
    _open_sheet(page, base_url, "unit-1")
    expect(page.locator("#detailTempDown")).to_be_disabled()
    expect(page.locator("#detailTempUp")).to_be_enabled()
    expect(page.locator("#detailSetTemp")).to_have_text("16.0°")
    page.locator("#detailDone").click()
    page.wait_for_timeout(300)
    assert posts == [], "the floor must not POST a sub-range value"


def test_fan_segment_posts_fan_speed(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_sheet(page, base_url, "unit-1")
    fan = page.locator("#detailFanSpeed")
    # The selected segment is the state; numbered speeds read as digits.
    expect(fan.locator('[aria-pressed="true"]')).to_have_text("Auto")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        fan.locator('[data-value="Three"]').click()
    assert info.value.post_data_json == {"fan_speed": "Three"}
    expect(fan.locator('[aria-pressed="true"]')).to_have_text("3")


def test_offline_unit_row_is_marked_and_inert(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """An unreachable unit says so and cannot be commanded (#520).

    The bug this guards: controls that look live but silently no-op because the
    unit lost its cloud connection.
    """
    sample_units[1]["reachable"] = False  # unit-2 (Studio)
    mock_api(sample_units)
    _boot(page, base_url)

    offline = _row(page, "unit-2")
    expect(offline.locator(".row-avatar")).to_have_attribute("data-badge", "down")
    expect(offline.locator(".ac-line-offline")).to_have_text("Offline")
    expect(offline.locator(".ac-line-toggle")).to_be_disabled()
    # The last-known reading stays on screen.
    expect(offline.locator(".action-row-meta")).to_contain_text("Last read 19.0")

    # A reachable sibling is untouched.
    online = _row(page, "unit-1")
    expect(online.locator(".ac-line-offline")).to_have_count(0)
    expect(online.locator(".ac-line-toggle")).to_be_enabled()


def test_offline_unit_row_marked_on_home_summary(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The Home rows mirror the AC tab's offline state (#520)."""
    sample_units[1]["reachable"] = False  # unit-2 (Studio)
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#acSummary .ac-row", state="visible")

    row = page.locator("#acSummary .ac-row", has_text="Studio")
    # The shared row (#880): the avatar's badge says it should be connected
    # and is not, and the meta line closes on the Offline chip.
    expect(row.locator(".row-avatar")).to_have_attribute("data-badge", "down")
    expect(row.locator(".ac-line-offline")).to_have_text("Offline")
    expect(row.locator(".ac-line-toggle")).to_be_disabled()
    # Only the offline unit is marked.
    expect(page.locator("#acSummary .ac-line-offline")).to_have_count(1)
    expect(page.locator("#acSummary .ac-line-toggle:enabled")).to_have_count(
        len(sample_units) - 1
    )


@pytest.mark.chromium_only
def test_row_meta_carries_the_rule_and_the_boost_chip(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """The rule's room target joins the meta line while it steers; a running
    solar boost is an accent chip with its signed offset (#554, #575)."""
    sample_units[0]["temperature_rule"] = {
        "enabled": True, "active_target": 23.0, "boost_active": False, "boost_delta_c": None,
    }
    sample_units[2]["temperature_rule"] = {
        "enabled": True, "active_target": 22.0, "boost_active": True, "boost_delta_c": -2.0,
    }
    mock_api(sample_units)
    _boot(page, base_url)
    expect(_row(page, "unit-1").locator(".action-row-meta")).to_contain_text("rule 23.0°")
    expect(_row(page, "unit-1").locator(".ac-line-boost")).to_have_count(0)
    boost = _row(page, "unit-3").locator(".ac-line-boost")
    expect(boost).to_have_text("Boost -2")
    expect(boost).to_have_attribute("data-tone", "accent")


def test_unit_row_target_is_44px_and_clear_of_its_switch(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    _boot(page, base_url)
    row = _row(page, "unit-1")
    main = effective_rect(row.locator(".action-row-main"))
    switch = effective_rect(row.locator(".ac-line-toggle"))
    assert main.effective.height >= 44
    assert switch.effective.height >= 44
    assert main.effective.right <= switch.effective.left
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_on_switch_track_is_the_accent_not_green(
    theme: str, page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """An on switch is the app's accent fill, never the success green (#818,
    fleet-config#1200). The expected colour is resolved from the live
    ``--accent-fill`` token so a P3 display (oklch) compares like for like."""
    mock_api(sample_units)
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.evaluate("t => localStorage.setItem('home-automation.theme', t)", theme)
    page.reload(wait_until="domcontentloaded")
    page.locator("#tabAc").click()
    page.wait_for_selector("#acUnits .ac-row", state="visible")
    assert page.evaluate("document.documentElement.dataset.theme") == theme

    track = page.locator('#acUnits [data-unit-id="unit-1"] .ac-line-toggle[aria-checked="true"]')
    expect(track).to_be_visible()
    got, accent, green = page.evaluate(
        """el => {
            const resolve = token => {
                const probe = document.createElement('i');
                probe.style.background = 'var(' + token + ')';
                document.body.appendChild(probe);
                const c = getComputedStyle(probe).backgroundColor;
                probe.remove();
                return c;
            };
            return [getComputedStyle(el).backgroundColor, resolve('--accent-fill'), resolve('--on')];
        }""",
        track.element_handle(),
    )
    assert got == accent, f"{theme}: on switch track {got} is not accent-fill {accent}"
    assert got != green, f"{theme}: on switch track is still the success green {green}"
