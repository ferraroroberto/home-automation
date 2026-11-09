"""The Network sheet's internet trends and health-row layout: a steady latency
keeps its shape, too few speed tests show a note, the two Reboot buttons never
touch."""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._flow import run_flow
from tests.e2e._geometry import apply_matrix_leg, assert_no_horizontal_overflow
from tests.e2e.test_network import open_network_sheet

_SPARK_PATH_Y = (
    r"el => (el.getAttribute('d').match(/[ML]\s*[\d.]+\s+([\d.]+)/g) || [])"
    r".map(m => parseFloat(m.trim().split(/\s+/).pop()))"
)


def _steady_latency_trend_keeps_its_shape_not_a_filled_block(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """A near-constant latency plots mid-chart with visible jitter and no area fill."""
    mock_api(sample_units)
    mock_energy()
    base = 1_699_900_000
    mock_network(internet_history={
        "latency": [[base + i * 900, 14 + (i % 2)] for i in range(96)],
        "download": [],
        "upload": [],
    })
    boot_home(page, base_url)
    open_network_sheet(page)

    chart = page.get_by_test_id("net-trends").locator(".net-spark").first
    expect(chart).to_be_visible()
    expect(chart.locator(".net-spark-area")).to_have_count(0)
    ys = chart.locator(".is-latency").evaluate(_SPARK_PATH_Y)
    assert len(ys) == 96
    # Neither pinned to an edge nor flat: the 1 ms wobble is drawn, mid-height.
    assert min(ys) > 8 and max(ys) < 32, ys[:4]
    assert max(ys) - min(ys) >= 3, ys[:4]


def _speed_trend_with_too_few_tests_shows_a_note_not_a_line(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network(internet_history={
        "latency": [],
        "download": [[1_699_000_000, 310.0], [1_699_500_000, 280.0]],
        "upload": [[1_699_000_000, 42.0], [1_699_500_000, 38.0]],
    })
    boot_home(page, base_url)
    open_network_sheet(page)

    trends = page.get_by_test_id("net-trends")
    expect(trends.locator(".net-trend-value").nth(1)).to_have_text("2 tests")
    expect(trends.locator(".net-trend-empty").nth(1)).to_contain_text("needs 3 tests")
    expect(trends.locator(".net-spark")).to_have_count(0)


def _reboot_buttons_are_separated_at_desktop_and_phone_widths(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """The AP and router Reboot buttons never touch (a gap side by side, stacked on a phone)."""
    mock_api(sample_units)
    mock_energy()
    mock_network()
    boot_home(page, base_url)
    open_network_sheet(page)
    ap = page.locator("#netApReboot")
    router = page.locator("#netRouterReboot")
    expect(ap).to_be_visible()
    expect(router).to_be_visible()

    for width, theme in ((1280, "dark"), (1280, "light"), (390, "dark"), (320, "light")):
        apply_matrix_leg(page, width, theme)
        a, r = ap.bounding_box(), router.bounding_box()
        assert a is not None and r is not None
        side_by_side = abs(a["y"] - r["y"]) < a["height"]
        if side_by_side:
            assert r["x"] - (a["x"] + a["width"]) >= 8, f"{width}px: buttons touch"
        else:
            assert r["y"] - (a["y"] + a["height"]) >= 8, f"{width}px: buttons touch"


@pytest.mark.chromium_only
def _network_tab_retries_after_first_load_failure(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network(failures_before_success=1)
    # The recovery comes from the tab's own 15 s poll (network.js POLL_MS).
    # A controlled clock fires that tick at once instead of waiting it out in
    # real time, which made this the suite's slowest test (#778).
    page.clock.install()
    boot_home(page, base_url)

    open_network_sheet(page)

    expect(page.locator("#netFeedback .empty-state-message")).to_have_text(
        "Network unavailable"
    )
    expect(page.locator("#netInternetStatus")).not_to_have_text("Online")
    page.clock.run_for(15_000)
    expect(page.locator("#netInternetStatus")).to_have_text("Online")
    expect(page.locator("#netFeedback")).to_be_hidden()
    expect(page.locator("#netDevices .net-device-name-text").first).to_have_text("Alpha Laptop")


def _internet_tile_trends_fit_the_phone_widths_in_both_themes(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """The latency + speed sparklines (#840) stay legible at 320/390px, light and dark."""
    mock_api(sample_units)
    mock_energy()
    mock_network()
    boot_home(page, base_url)
    open_network_sheet(page)

    trends = page.get_by_test_id("net-trends")
    expect(trends.locator(".net-trend")).to_have_count(2)
    expect(trends.locator(".net-spark")).to_have_count(2)
    expect(trends.locator(".net-trend-value").first).to_have_text("20 ms")
    expect(trends.locator(".net-trend-value").nth(1)).to_have_text("3 tests")

    strokes: Dict[str, str] = {}
    for width in (320, 390):
        for theme in ("light", "dark"):
            apply_matrix_leg(page, width, theme)
            tile = page.locator("section.net-internet")
            tile_box = tile.bounding_box()
            assert tile_box is not None
            for chart in trends.locator(".net-spark").all():
                box = chart.bounding_box()
                assert box is not None
                # Wide enough to read a trend, and inside the tile's right edge.
                assert box["width"] >= 120, f"{width}px/{theme}: sparkline {box['width']}px"
                assert box["x"] + box["width"] <= tile_box["x"] + tile_box["width"] + 0.5
            for row in trends.locator(".net-trend").all():
                row_box = row.bounding_box()
                assert row_box is not None and row_box["height"] >= 44
            assert_no_horizontal_overflow(page)
            strokes[theme] = page.evaluate(
                "getComputedStyle(document.querySelector('.net-spark .is-latency')).stroke"
            )
            assert strokes[theme] not in ("none", "")
    # The line takes the theme's accent token — it changes with the theme, no redraw.
    assert strokes["light"] != strokes["dark"]
    # Upload is dashed so the two speed lines differ without relying on colour.
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.net-spark .is-up')).strokeDasharray"
    ) not in ("none", "")


def _internet_tile_trends_explain_themselves_when_empty(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network(internet_history={"latency": [], "download": [], "upload": []})
    boot_home(page, base_url)
    open_network_sheet(page)

    trends = page.get_by_test_id("net-trends")
    expect(trends.locator(".net-trend-empty").first).to_contain_text("tab is open")
    expect(trends.locator(".net-trend-empty").nth(1)).to_contain_text("Run a speed test")
    expect(trends.locator(".net-spark")).to_have_count(0)


def _nightly_speedtest_switch_is_off_by_default_persists_and_fits_the_phone(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """The opt-in nightly speed test (#840): off until switched on, saved server-side."""
    mock_api(sample_units)
    mock_energy()
    mock_network()
    boot_home(page, base_url)
    open_network_sheet(page)

    switch = page.get_by_test_id("net-nightly")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(switch.locator(".toggle-label")).to_have_text("OFF")
    expect(page.locator(".net-nightly-hint")).to_contain_text("Opt-in")

    with page.expect_request(
        lambda r: r.method == "PUT" and r.url.endswith("/api/network/speedtest-prefs")
    ) as sent:
        switch.click()
    assert sent.value.post_data_json == {"nightly_enabled": True}
    expect(switch).to_have_attribute("aria-checked", "true")

    # A fresh load reads the saved value back rather than assuming the default.
    page.reload(wait_until="domcontentloaded")
    # The tab is remembered: the reload lands on Devices. The saved preference
    # is read when the sheet opens.
    page.locator("#networkInternetOpen").click()
    expect(page.get_by_test_id("net-nightly")).to_have_attribute("aria-checked", "true")

    for width in (320, 390):
        for theme in ("light", "dark"):
            apply_matrix_leg(page, width, theme)
            tile_box = page.locator("section.net-internet").bounding_box()
            sw_box = page.get_by_test_id("net-nightly").bounding_box()
            assert tile_box is not None and sw_box is not None
            assert sw_box["x"] + sw_box["width"] <= tile_box["x"] + tile_box["width"] + 0.5
            assert sw_box["x"] >= tile_box["x"]
            expect(page.locator(".net-nightly-hint")).to_be_visible()
            assert_no_horizontal_overflow(page)


def test_network_trends(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """Internet tile: trend shapes, empty-trend notes, phone widths in both themes, reboot buttons, the nightly speed-test switch, then the retry after a failed first load (last: the page clock stays installed)."""
    run_flow(
        request, page, base_url,
        _steady_latency_trend_keeps_its_shape_not_a_filled_block,
        _speed_trend_with_too_few_tests_shows_a_note_not_a_line,
        _reboot_buttons_are_separated_at_desktop_and_phone_widths,
        _internet_tile_trends_fit_the_phone_widths_in_both_themes,
        _internet_tile_trends_explain_themselves_when_empty,
        _nightly_speedtest_switch_is_off_by_default_persists_and_fits_the_phone,
        _network_tab_retries_after_first_load_failure,
    )
