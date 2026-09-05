"""Energy tab — the live flow row, the charts, and the solar diagnostics.

Flow/chart render, the pane's loading/unavailable/stale states, the chart
tick budget and non-colour series cues, the feed-outage note (#579) and the
sun-position overlay (#590).
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import chart_dataset_cues, chart_tick_budget


def test_energy_tab_renders_flow_and_charts(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()  # default fixture: PV 2500 W, house 1300 W, export 1200 W
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    # Flow row: Solar / Home / Grid live values, with thousands separators.
    expect(page.locator("#flowPv")).to_have_text("2,500 W")
    expect(page.locator("#flowHouse")).to_have_text("1,300 W")
    expect(page.locator("#flowGrid")).to_have_text("1,200 W")
    # Exporting (surplus > 0) → the grid arrow points out (▶) and reads as export.
    grid_arrow = page.locator("#wireGrid")
    expect(grid_arrow).to_have_class("flow-arrow is-export")
    expect(grid_arrow).to_have_text("▶")

    # Today's generation card is populated from /api/energy/today.
    expect(page.locator("#genTotal")).to_have_text("9.00 kWh")
    # Savings €: now the tiered avoided-cost from /api/energy/cost?range=day
    # (the cost fixture's totals.savings), not the old flat-rate computation.
    expect(page.locator("#savEur")).to_have_text("€0.37")

    # Both chart canvases render once the pane is shown.
    expect(page.locator("#liveChart")).to_be_visible()
    expect(page.locator("#aggChart")).to_be_visible()
    expect(page.locator("#exportCreditChart")).to_be_visible()
    expect(page.locator("#energySummary")).to_contain_text("Solar consumed")
    expect(page.locator("#costSummary")).to_contain_text("Total solar benefit")
    assert page.locator("#aggChart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(dataset => dataset.label)"
    ) == ["Production", "Consumption", "Grid imported", "Solar consumed", "Solar exported"]
    assert page.locator("#exportCreditChart").evaluate(
        "canvas => Chart.getChart(canvas).data.datasets.map(dataset => dataset.label)"
    ) == ["Grid cost", "Avoided cost", "Export income"]

    # Cost & savings breakdown table: a row per tariff period + a Total row,
    # fed by the /api/energy/cost stub.
    expect(page.locator("#costBody tr")).to_have_count(3)
    expect(page.locator("#costFoot")).to_contain_text("Total")
    expect(page.locator("#costFoot")).to_contain_text("€0.37")

    page.locator("#exportRateCard summary").click()
    expect(page.locator("#exportRateCurrent")).to_have_text("€0.05000/kWh")
    page.locator("#exportRateDate").fill("2026-09-05")
    page.locator("#exportRateAdd").click()
    expect(page.locator("#exportRateError")).to_have_text("Enter an export rate.")
    expect(page.locator("#exportRateValue")).to_be_focused()
    expect(page.locator("#exportRateList")).not_to_contain_text("2026-09-05")

    page.locator("#exportRateValue").fill("0.16774")
    page.locator("#exportRateAdd").click()
    expect(page.locator("#exportRateList")).to_contain_text("2026-09-05")
    expect(page.locator("#exportRateCurrent")).to_have_text("€0.16774/kWh")

    page.locator(".export-rate-edit[data-rate-date='2026-09-05']").click()
    expect(page.locator("#exportRateAdd")).to_have_text("Save changes")
    page.locator("#exportRateValue").fill("0.18000")
    page.locator("#exportRateAdd").click()
    expect(page.locator("#exportRateList")).to_contain_text("€0.18000 / kWh")

    page.locator(".export-rate-edit[data-rate-date='2026-09-05']").click()
    page.locator("#exportRateDelete").click()
    expect(page.locator("#confirmDialog")).to_be_visible()
    page.locator("#confirmOk").click()
    expect(page.locator("#exportRateList")).not_to_contain_text("2026-09-05")


def test_energy_tab_shows_loading_before_first_live_result(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/energy' || url.endsWith('/api/energy')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    expect(page.locator("#paneEnergy")).to_have_attribute("data-state", "loading")
    expect(page.locator("#energyFeedback .empty-state-message")).to_have_text(
        "Reading live energy…"
    )
    expect(page.locator("#paneEnergy")).to_have_attribute("data-state", "ready")
    expect(page.locator("#energyFeedback")).to_be_hidden()


def test_energy_tab_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable,
) -> None:
    mock_api(sample_units)
    page.route(
        "**/api/energy",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"FusionSolar portal timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    expect(page.locator("#paneEnergy")).to_have_attribute("data-state", "error")
    expect(page.locator("#energyFeedback .empty-state-message")).to_have_text(
        "Live energy unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.90")


def test_energy_poll_failure_preserves_and_labels_last_good_flow(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()
    expect(page.locator("#flowPv")).to_have_text("2,500 W")

    page.route(
        "**/api/energy",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"FusionSolar portal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabAc").click()
    page.locator("#tabEnergy").click()

    expect(page.locator("#paneEnergy")).to_have_attribute("data-state", "stale")
    expect(page.locator("#flowPv")).to_have_text("2,500 W")
    expect(page.locator("#energyFeedback")).to_contain_text("Last updated")
    expect(page.locator("#energyFeedback")).to_contain_text("live data unavailable")
    expect(page.locator("#energyFeedback")).not_to_contain_text("192.0.2.90")


def test_energy_chart_tick_budget_updates_with_viewport(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    samples = [
        {
            "ts": 1_700_000_000 + index * 300,
            "pv_power_w": 2_000.0 + index * 10,
            "house_consumption_w": 1_200.0,
            "grid_import_w": 0.0,
            "grid_export_w": 800.0 + index * 10,
            "pv_surplus_w": 800.0 + index * 10,
            "inverter_reachable": True,
            "meter_reachable": True,
        }
        for index in range(24)
    ]
    buckets = [
        {
            "key": f"2026-06-19T{index:02d}",
            "label": f"{index:02d}:00",
            "pv_wh": 1_800.0,
            "house_wh": 1_100.0,
            "import_wh": 0.0,
            "export_wh": 700.0,
            "pv_n": 60,
            "pv_missing": False,
        }
        for index in range(24)
    ]
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy(samples=samples, buckets=buckets)
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    page.wait_for_function(
        "() => Chart.getChart(document.querySelector('#liveChart'))?.data.labels.length >= 24"
    )
    assert chart_tick_budget(page, "#liveChart").max_ticks_limit == 4

    page.set_viewport_size({"width": 772, "height": 844})
    page.wait_for_function(
        "() => Chart.getChart(document.querySelector('#liveChart'))"
        ".options.scales.x.ticks.maxTicksLimit === 8"
    )


def test_energy_series_have_non_colour_visual_cues(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()
    page.wait_for_function(
        "() => Chart.getChart(document.querySelector('#liveChart'))?.data.datasets.length === 3"
    )

    cues = chart_dataset_cues(page, "#liveChart")
    assert [(c.label, c.border_dash, c.point_style) for c in cues] == [
        ("Generation", [], "circle"),
        ("Grid-supplied", [8, 4], "rectRot"),
        ("Consumption", [2, 4], "triangle"),
    ]


def test_a_feed_outage_is_visible_as_an_outage_not_a_collapse(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#579: the day the PV feed died must not read as the day the array died.

    The whole point of the issue is that this distinction is *visible*, so it
    is asserted where a user would see it — the note under today's generation,
    the forecast card's meta line, and the forecast overlay drawing the
    under-covered hours as hollow, dashed projections rather than solid points.
    """
    actual = [
        {"hour": h, "wh": 900.0, "measured_wh": 900.0,
         "estimated": False, "partial": False, "coverage": 0.98}
        for h in range(24)
    ]
    # 09:00 measured only a quarter of its hour; 10:00 half of it.
    actual[9] = {"hour": 9, "wh": 3400.0, "measured_wh": 850.0,
                 "estimated": True, "partial": False, "coverage": 0.25}
    actual[10] = {"hour": 10, "wh": 4600.0, "measured_wh": 2300.0,
                  "estimated": True, "partial": False, "coverage": 0.5}

    mock_api(sample_units)
    mock_energy(
        forecast={"actual": actual, "actual_gap_hours": 1.25},
        today_gap_hours=1.25,
    )
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    gap = page.locator("#genGap")
    expect(gap).to_be_visible()
    expect(gap).to_contain_text("1.3 h")
    expect(page.locator("#forecastMeta")).to_contain_text("feed offline")

    page.wait_for_function(
        "() => Chart.getChart(document.querySelector('#forecastChart'))"
        "?.data.datasets[1].data.filter(v => v != null).length === 24"
    )
    marks = page.evaluate(
        "() => {"
        "  const ds = Chart.getChart(document.querySelector('#forecastChart'))"
        "    .data.datasets[1];"
        "  return {"
        "    marked: ds.pointRadius.map((r, i) => r > 0 ? i : -1).filter(i => i >= 0),"
        "    hollow: ds.pointBackgroundColor,"
        "    dashedAtGap: String(ds.segment.borderDash({ p1DataIndex: 9 })),"
        "    dashedAtGood: String(ds.segment.borderDash({ p1DataIndex: 13 })),"
        "  };"
        "}"
    )
    assert marks["marked"] == [9, 10]        # only the under-covered hours
    assert marks["hollow"] == "transparent"  # …drawn as projections
    assert marks["dashedAtGap"] == "4,3"
    assert marks["dashedAtGood"] == "undefined"


def test_sun_position_diagnostic_plots_measured_pr_and_names_what_it_dropped(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#590: the divergence is readable as geometry, and gaps are never plotted.

    Asserted where a user would see it: the card is folded away and costs
    nothing until opened, the measured points land at their sun azimuths beside
    the flat modelled reference, and the hours the feed only partly reached are
    named in the note rather than quietly drawn as a low performance ratio.
    """
    points = [
        {"hour": 11, "azimuth_deg": 165.0, "elevation_deg": 62.0,
         "effective_pr": 0.785, "actual_wh": 3140.0, "expected_wh": 3200.0,
         "gti_w_m2": 800.0, "coverage": 0.98},
        {"hour": 15, "azimuth_deg": 245.0, "elevation_deg": 41.0,
         "effective_pr": 0.551, "actual_wh": 1500.0, "expected_wh": 2180.0,
         "gti_w_m2": 545.0, "coverage": 0.98},
        {"hour": 17, "azimuth_deg": 285.0, "elevation_deg": 22.0,
         "effective_pr": 0.178, "actual_wh": 260.0, "expected_wh": 1170.0,
         "gti_w_m2": 292.0, "coverage": 0.98},
    ]
    mock_api(sample_units)
    mock_energy(sun_overlay={
        "points": points,
        "excluded": [{"hour": 9, "reason": "coverage"},
                     {"hour": 10, "reason": "coverage"},
                     {"hour": 19, "reason": "no_data"}],
        "excluded_coverage": 2,
        "excluded_no_data": 1,
        "modelled_pr": 0.8,
    })
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()

    card = page.locator("#sunOverlayCard")
    # Folded away by default — nothing is fetched or drawn until asked for.
    expect(card).not_to_have_attribute("open", "")
    assert page.evaluate(
        "() => !Chart.getChart(document.querySelector('#sunOverlayChart'))"
    )

    card.locator("summary").click()
    page.wait_for_function(
        "() => Chart.getChart(document.querySelector('#sunOverlayChart'))"
        "?.data.datasets[0].data.length === 3"
    )

    plotted = page.evaluate(
        "() => {"
        "  const c = Chart.getChart(document.querySelector('#sunOverlayChart'));"
        "  return {"
        "    measured: c.data.datasets[0].data.map(p => [p.x, p.y]),"
        "    modelled: c.data.datasets[1].data.map(p => [p.x, p.y]),"
        "  };"
        "}"
    )
    # The measured points sit at their own sun azimuths, falling away as the sun
    # moves west — the signature the card exists to make self-service.
    assert plotted["measured"] == [[165, 0.785], [245, 0.551], [285, 0.178]]
    # …against one flat modelled reference spanning the plotted azimuths.
    assert plotted["modelled"] == [[165, 0.8], [285, 0.8]]

    # The dropped hours are named, not silently absent — and the two kinds are
    # told apart, because a partly-covered hour and a never-measured one mean
    # different things to whoever is reading the day.
    note = page.locator("#sunOverlayNote")
    expect(note).to_contain_text("3 hours plotted")
    expect(note).to_contain_text("2 hours excluded — feed coverage too short")
    expect(note).to_contain_text("1 daylight hour never measured")
    expect(page.locator("#sunOverlayEmpty")).to_be_hidden()


def test_sun_position_diagnostic_empty_day_is_an_empty_state_not_an_error(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A day the app was not running for has no rollups — that is not an error."""
    mock_api(sample_units)
    mock_energy(sun_overlay={"points": [], "excluded": [], "excluded_coverage": 0})
    boot_home(page, base_url)
    page.locator("#tabEnergy").click()
    page.locator("#sunOverlayCard summary").click()

    empty = page.locator("#sunOverlayEmpty")
    expect(empty).to_be_visible()
    expect(empty).to_contain_text("No measured hours")
    expect(page.locator("#sunOverlayNote")).to_contain_text("0 hours plotted")
