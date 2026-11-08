"""The Network sheet's internet trends and health-row layout: a steady latency
keeps its shape, too few speed tests show a note, the two Reboot buttons never
touch."""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import apply_matrix_leg
from tests.e2e.test_network import open_network_sheet

_SPARK_PATH_Y = (
    r"el => (el.getAttribute('d').match(/[ML]\s*[\d.]+\s+([\d.]+)/g) || [])"
    r".map(m => parseFloat(m.trim().split(/\s+/).pop()))"
)


def test_steady_latency_trend_keeps_its_shape_not_a_filled_block(
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


def test_speed_trend_with_too_few_tests_shows_a_note_not_a_line(
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


def test_reboot_buttons_are_separated_at_desktop_and_phone_widths(
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
