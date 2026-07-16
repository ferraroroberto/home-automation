"""Tab navigation + the new Home summary and Energy dashboard.

Covers the Home / AC / Energy switcher, the read-only AC summary on Home, and
an Energy-tab render (the live flow row + charts) against stubbed energy
fixtures — on both the Chromium-desktop and WebKit/iPhone projections.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Locator, Page, expect

from tests.e2e._geometry import (
    EffectiveRect,
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    chart_dataset_cues,
    chart_tick_budget,
    effective_rects,
)


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


def _stable_effective_rects(locator: Locator) -> List[EffectiveRect]:
    """Wait for the first match to be visible, then measure. Under full-suite
    load a tab click's re-render can still land the read mid-repaint, so
    retry once if a rect comes back implausibly small (#431)."""
    expect(locator.first).to_be_visible()
    rects = effective_rects(locator)
    if any(r.effective.width < 1 or r.effective.height < 1 for r in rects):
        expect(locator.first).to_be_visible()
        rects = effective_rects(locator)
    return rects


def test_tab_navigation_switches_panes(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    _boot(page, base_url)

    # Default: Home visible, the other panes hidden.
    expect(page.locator("#paneHome")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy")).to_be_hidden()

    # Home now shows the same Solar → Home ← Grid flow card as Energy (issue #57),
    # revealed once the first /api/energy read lands.
    expect(page.locator("#paneHome .flow-row")).to_be_visible()
    expect(page.locator("#homeFlowPv")).to_have_text("2,500 W")

    # AC tab → unit cards become visible.
    page.locator("#tabAc").click()
    expect(page.locator("#paneAc")).to_be_visible()
    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator(".unit-card").first).to_be_visible()

    # Energy tab → the live flow row shows.
    page.locator("#tabEnergy").click()
    expect(page.locator("#paneEnergy")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy .flow-row")).to_be_visible()


# Computed transform of the floating nav is at its locked rest position iff it
# has no upward translate — 'none' (cleared) or the identity matrix.
_NAV_AT_REST = (
    "() => { const t = getComputedStyle(document.querySelector('.tabs')).transform;"
    " return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'; }"
)


def test_nav_self_heals_when_stranded(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
) -> None:
    """#229: a latched upward transform on the floating bottom-tab pill must be
    repainted back to its locked bottom position by the self-healing watchdog,
    with no app restart. Playwright's WebKit doesn't reproduce iOS Safari's
    collapsing toolbar, so we inject the exact failure mode — a stale
    ``translateY(-Npx)`` — directly, then assert the controller re-derives the
    resting position and clears it."""
    mock_api(sample_units)
    _boot(page, base_url)

    nav = page.locator(".tabs")
    # Set + read in one evaluate call so the watchdog can't interleave between them.
    strand_transform = nav.evaluate(
        "el => { el.style.transform = 'translateY(-120px)';"
        " return getComputedStyle(el).transform; }"
    )
    assert "120" in strand_transform

    # The ~400ms watchdog re-derives the rest position and clears the strand.
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_nav_not_left_translated_after_modal(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
) -> None:
    """#229: opening then closing a detail modal must leave the nav at rest —
    no stranded transform — via both the X button and the Esc key (the path
    that never routed through the app's close handlers and historically left
    the bar stuck up)."""
    mock_api(sample_units)
    _boot(page, base_url)
    page.locator("#tabAc").click()
    page.wait_for_selector(".unit-card", state="visible")

    # Close via the X button.
    page.locator('[data-unit-id="unit-1"] .unit-header').click()
    expect(page.locator("#detailDialog")).to_be_visible()
    page.locator("#detailClose").click()
    expect(page.locator("#detailDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)

    # Close via Esc.
    page.locator('[data-unit-id="unit-1"] .unit-header').click()
    expect(page.locator("#detailDialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#detailDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_app_restores_saved_short_tab_with_nav_at_rest(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#232: the nav is a body-level sibling of the inner scroller, so the PWA
    can safely restore a short saved tab without floating the fixed bar up."""
    page.add_init_script(
        "localStorage.setItem('home-automation.tab', 'plugs');"
    )
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#panePlugs", state="visible")

    expect(page.locator("body > .tabs")).to_have_count(1)
    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator("#tabPlugs")).to_have_attribute("aria-selected", "true")
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_nav_at_rest_after_plug_modal_with_autofocus(
    page: Page, base_url: str, sample_plugs: List[Dict], mock_tuya: Callable,
) -> None:
    """#229 follow-up: the plugs rename modal auto-focuses its Display-name input
    (plugs.js), which raises the iOS keyboard and shrinks the visual viewport —
    the one path that still stranded the nav. Opening it (input focused) then
    closing must leave the bar at its locked rest position."""
    mock_tuya(sample_plugs)
    _boot(page, base_url)
    page.locator("#tabPlugs").click()
    page.wait_for_selector("#panePlugs", state="visible")
    # Rows live inside collapsed <details> cards — expand so they're interactable.
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )

    page.locator('[data-device-id="plug-1"] .device-row-name').click()
    expect(page.locator("#plugDialog")).to_be_visible()
    # The modal auto-focuses the text input — assert that, then close.
    expect(page.locator("#plugDisplayName")).to_be_focused()
    page.locator("#plugDetailClose").click()
    expect(page.locator("#plugDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_app_padding_owned_by_vendored_nav_on_mobile(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#420: on the mobile-pill breakpoint the vendored nav-tabs.css owns
    .app's padding (safe-area top + nav-pill bottom clearance). It loads
    BEFORE styles.css, so an unconditioned ``padding`` shorthand on the base
    .app rule silently wins the cascade and flattens both — the PWA then
    renders under the iOS status bar and behind the floating pill."""
    mock_api(sample_units)
    mock_energy()
    _boot(page, base_url)

    metrics = page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('.app'));"
        " return { mobilePill: matchMedia('(pointer: coarse) and (max-width: 520px)').matches,"
        " top: parseFloat(s.paddingTop), bottom: parseFloat(s.paddingBottom) }; }"
    )
    if metrics["mobilePill"]:
        # env(safe-area-inset-*) is 0 under emulation, so the floors are
        # --gap (12px) on top and margin+bar+margin+gap (≈115px) below.
        assert metrics["top"] >= 12, metrics
        assert metrics["bottom"] >= 100, metrics
    else:
        # Desktop keeps the styles.css padding: 0 var(--gap) 24px.
        assert metrics["top"] == 0, metrics
        assert metrics["bottom"] == 24, metrics


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
    _boot(page, base_url)
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
    _boot(page, base_url)

    lines = page.locator("#acSummary .ac-line")
    expect(lines).to_have_count(len(sample_units))
    # One scannable line per unit: name + an actionable power toggle (issue #72).
    expect(page.locator("#acSummary")).to_contain_text("Office")
    expect(page.locator("#acSummary .ac-line-toggle")).to_have_count(len(sample_units))


def test_vm_tile_distinguishes_loading_from_not_found(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/hyperv' || url.endsWith('/api/hyperv')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({
                  hyperv: {available: false, state: 'not_found'}
                }), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    _boot(page, base_url)

    # #461: the summary row is the whole VM surface — status text + switch.
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "loading")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("Reading status…")
    expect(page.locator("#homeVmToggle")).to_be_disabled()
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "empty")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("VM not found")
    expect(page.locator("#homeVmToggle")).to_be_disabled()


def test_vm_tile_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Hyper-V host 192.0.2.80 timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("status unavailable")
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.80")


def test_vm_status_error_keeps_start_action_when_vm_is_identified(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": False,
                    "name": "Fixture HA",
                    "state": "unknown",
                    "error": "Get-VM status failed",
                }
            }),
        ),
    )
    page.route(
        "**/api/hyperv/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": True,
                    "name": "Fixture HA",
                    "state": "running",
                    "uptime_seconds": 0,
                }
            }),
        ),
    )
    _boot(page, base_url)

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    # An unreachable-but-identified VM keeps the summary switch usable for
    # start (#461: the switch replaced the old tile's "Start Home Assistant").
    toggle = page.locator("#homeVmToggle")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "true")
    # The switch lives inside the card's <summary>: clicking it must never
    # fold or unfold the card.
    expect(page.locator("#homeAssistantCard")).not_to_have_attribute("open", "")


def test_vm_command_failure_uses_concise_toast(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": False,
                    "name": "Fixture HA",
                    "state": "unknown",
                }
            }),
        ),
    )
    page.route(
        "**/api/hyperv/start",
        lambda route: route.fulfill(
            status=502,
            content_type="application/json",
            body='{"detail":"Start-VM host 192.0.2.80 Value cannot be null Parameter name: name"}',
        ),
    )
    _boot(page, base_url)
    page.locator("#homeVmToggle").click()

    expect(page.locator("#toast")).to_have_text("Couldn't start Home Assistant")
    expect(page.locator("#toast")).not_to_contain_text("Start-VM")
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.80")


def test_vm_poll_failure_preserves_status_and_disables_power(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    failing = {"value": False}
    vm = {
        "available": True,
        "state": "running",
        "uptime_seconds": 3600,
        "ip_address": "192.0.2.81",
    }

    def handle_vm(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"Hyper-V host 192.0.2.80 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"hyperv": vm}),
        )

    page.route("**/api/hyperv", handle_vm)
    _boot(page, base_url)
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(page.locator("#homeVmToggle")).to_be_enabled()

    failing["value"] = True
    page.locator("#tabAc").click()
    page.locator("#tabHome").click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "stale")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("cached")
    expect(page.locator("#homeVmToggle")).to_be_disabled()
    # The stale detail moved into the status text's tooltip (#461).
    expect(page.locator("#homeAssistantSummaryState")).to_have_attribute(
        "title", re.compile("Last updated .+ · live data unavailable")
    )
    expect(page.locator("#homeAssistantSummaryState")).not_to_contain_text("192.0.2.80")


def test_ac_tab_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, mock_energy: Callable,
) -> None:
    mock_energy()
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/units' || url.endsWith('/api/units')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({units: []}), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    _boot(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "loading")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "Reading AC units…"
    )
    expect(page.locator("#paneAc")).to_have_attribute("data-state", "empty")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "No AC units configured"
    )


def test_ac_tab_shows_contextual_unavailable_state(
    page: Page, base_url: str, mock_energy: Callable,
) -> None:
    mock_energy()
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"melcloud.example.internal timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "error")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "AC units unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("melcloud.example.internal")


def test_ac_poll_failure_preserves_units_and_disables_actions(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    _boot(page, base_url)
    page.locator("#tabAc").click()
    expect(page.locator(".unit-card")).to_have_count(len(sample_units))

    page.unroute("**/api/units")
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"melcloud.example.internal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabEnergy").click()
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "stale")
    expect(page.locator("#acFeedback")).to_contain_text("Last updated")
    expect(page.locator("#acFeedback")).to_contain_text("live data unavailable")
    expect(page.locator(".unit-card")).to_have_count(len(sample_units))
    expect(page.locator("#unitsGrid button:enabled, #unitsGrid select:enabled")).to_have_count(0)
    expect(page.locator("#acSummary .ac-line-toggle:enabled")).to_have_count(0)
    expect(page.locator("#acFeedback")).not_to_contain_text("melcloud.example.internal")


def test_units_snapshot_paints_before_live_refresh(
    page: Page, base_url: str, sample_units: List[Dict], mock_energy: Callable,
) -> None:
    cached_units = copy.deepcopy(sample_units)
    live_units = copy.deepcopy(sample_units)
    cached_units[0]["name"] = "Snapshot Office"
    live_units[0]["name"] = "Live Office"
    snapshot_store = {
        "version": 1,
        "snapshots": {
            "units": {
                "saved_at": "2026-06-24T20:15:00.000Z",
                "body": {"units": cached_units},
            },
        },
    }
    page.add_init_script("""
        const snapshotStore = %s;
        const liveUnits = %s;
        localStorage.setItem('home-automation.apiSnapshots.v1', JSON.stringify(snapshotStore));
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/units' || url.endsWith('/api/units')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({units: liveUnits}), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 1000);
            });
          }
          return originalFetch(input, init);
        };
    """ % (json.dumps(snapshot_store), json.dumps(live_units)))
    mock_energy()
    _boot(page, base_url)

    expect(page.locator("#acSummary")).to_contain_text("Snapshot Office")
    expect(page.locator(".snapshot-badge").first).to_contain_text("Last saved")

    expect(page.locator("#acSummary")).to_contain_text("Live Office", timeout=4000)
    expect(page.locator("#acSummary")).not_to_contain_text("Snapshot Office")
    expect(page.locator(".snapshot-badge")).to_have_count(0)


def test_energy_tab_renders_flow_and_charts(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()  # default fixture: PV 2500 W, house 1300 W, export 1200 W
    _boot(page, base_url)
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

    # Cost & savings breakdown table: a row per tariff period + a Total row,
    # fed by the /api/energy/cost stub.
    expect(page.locator("#costBody tr")).to_have_count(3)
    expect(page.locator("#costFoot")).to_contain_text("Total")
    expect(page.locator("#costFoot")).to_contain_text("€0.37")


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
    _boot(page, base_url)
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
            body='{"detail":"SMA meter 192.0.2.90 timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)
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
    _boot(page, base_url)
    page.locator("#tabEnergy").click()
    expect(page.locator("#flowPv")).to_have_text("2,500 W")

    page.route(
        "**/api/energy",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"SMA meter 192.0.2.90 timed out after 10 seconds"}',
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
    _boot(page, base_url)
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
    _boot(page, base_url)
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


def test_security_tab_shows_loading_before_first_result(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/security' || url.endsWith('/api/security')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "loading")
    expect(page.locator("#securityFeedback .empty-state-message")).to_have_text(
        "Reading security status…"
    )
    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "ready")
    expect(page.locator("#securityState")).to_contain_text("Not armed")


def test_security_tab_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/security",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"risco.example.internal timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "error")
    expect(page.locator("#securityFeedback .empty-state-message")).to_have_text(
        "Security unavailable"
    )
    expect(page.locator("#securityState")).to_be_hidden()
    expect(page.locator("#toast")).not_to_contain_text("risco.example.internal")


def test_security_poll_failure_preserves_state_and_disables_actions(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)
    expect(page.locator("#homeSecurityState")).to_contain_text("Not armed")

    page.route(
        "**/api/security",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"risco.example.internal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "stale")
    expect(page.locator("#securityFeedback")).to_contain_text("Last updated")
    expect(page.locator("#securityFeedback")).to_contain_text("live data unavailable")
    expect(page.locator("#securityState")).to_contain_text("Not armed")
    expect(page.locator("#securityActions .security-action:enabled")).to_have_count(0)
    expect(page.locator("#homeSecurityActions .security-action:enabled")).to_have_count(0)
    expect(page.locator("#securityFeedback")).not_to_contain_text(
        "risco.example.internal"
    )


def test_cameras_distinguish_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"cameras":[]}',
        ),
    )
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/cameras' || url.endsWith('/api/cameras')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Reading cameras…"
    )
    expect(page.locator("#camerasList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "No cameras configured"
    )


def test_cameras_show_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "error")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Cameras unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.50")


def test_camera_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    failing = {"value": False}
    camera = {
        "id": "front-door",
        "display_name": "Front door",
        "reachable": True,
        "model": "Fixture camera",
        "recording": False,
        "ptz_presets": False,
        "ptz_absolute": False,
    }

    def handle_cameras(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"cameras": [camera]}),
        )

    page.route("**/api/cameras", handle_cameras)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)

    failing["value"] = True
    page.locator("#tabHome").click()
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)
    expect(page.locator("#camerasNote")).to_contain_text("Last updated")
    expect(page.locator("#camerasNote")).to_contain_text("live data unavailable")
    expect(page.locator("#camerasNote")).not_to_contain_text("192.0.2.50")


def test_presence_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence({
        "available": True,
        "total_count": 0,
        "located_count": 0,
        "home_count": 0,
        "away_count": 0,
        "unknown_count": 0,
        "all_away": False,
        "home_radius_m": 200,
        "entities": [],
        "diagnostics": {
            "available": True,
            "reason": "ok",
            "detail": "",
            "refreshed_at": "2026-06-22T10:00:00+00:00",
        },
    })
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/presence' || url.endsWith('/api/presence')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "Reading presence…"
    )
    expect(page.locator("#presenceList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "No presence entities configured"
    )


def test_presence_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/presence",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"icloud.example.internal timed out after 10 seconds"}',
        ),
    )
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "error")
    expect(page.locator("#presenceList .empty-state-message")).to_have_text(
        "Presence unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("icloud.example.internal")


def test_presence_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    expect(page.locator("#presenceList .presence-row")).to_have_count(3)

    page.route(
        "**/api/presence",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"icloud.example.internal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabHome").click()
    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#presenceList .presence-row")).to_have_count(3)
    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator("#presenceNote")).to_contain_text("Last updated")
    expect(page.locator("#presenceNote")).to_contain_text("live data unavailable")
    expect(page.locator("#presenceKidsHome")).to_be_disabled()
    expect(page.locator("#presenceNote")).not_to_contain_text(
        "icloud.example.internal"
    )


def test_presence_settings_use_compact_right_aligned_controls(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("details.presence-card > summary").click()
    page.locator("details.presence-settings-card > summary").click()

    control_ids = [
        "locationLabel",
        "locationLat",
        "locationLon",
        "presenceAutoEnabled",
        "presenceArmMinutes",
        "presenceStaleMinutes",
        "presenceDisarmOnArrival",
    ]
    boxes = {
        control_id: page.locator(f"#{control_id}").bounding_box()
        for control_id in control_ids
    }
    assert all(box is not None for box in boxes.values())
    right_edges = {
        round(box["x"] + box["width"])
        for box in boxes.values()
        if box is not None
    }
    assert len(right_edges) == 1, boxes

    assert boxes["locationLabel"]["width"] == boxes["locationLat"]["width"]
    assert boxes["locationLabel"]["width"] == boxes["locationLon"]["width"]
    assert boxes["locationLat"]["width"] <= 144
    assert boxes["locationLon"]["width"] <= 144
    assert boxes["presenceArmMinutes"]["width"] <= 88
    assert boxes["presenceStaleMinutes"]["width"] <= 88

    rows = page.locator(".presence-settings > .row")
    for index in range(rows.count()):
        label_box = rows.nth(index).locator(":scope > span").bounding_box()
        control_box = rows.nth(index).locator(":scope > input, :scope > button").bounding_box()
        assert label_box is not None and control_box is not None
        assert label_box["x"] + label_box["width"] <= control_box["x"] - 8


def test_security_tab_renders_presence_spike(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)

    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_be_visible()
    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator(".presence-row")).to_have_count(3)
    expect(page.locator(".presence-row.is-home")).to_contain_text("Home Phone")
    expect(page.locator(".presence-row.is-away")).to_contain_text("Away Phone")
    expect(page.locator(".presence-row.is-unknown")).to_contain_text("Keys")


def test_security_tab_adds_alarm_schedule(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)

    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .security-schedules-card > summary").click()
    page.locator("#securityScheduleAdd").click()

    dialog = page.locator("#securityScheduleDialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#securitySchedules .automation-summary-row")).to_have_count(0)
    page.locator("#securityScheduleTime").fill("22:30")
    page.locator("#securityScheduleAction").select_option("perimeter")
    dialog.locator(".alarm-schedule-day", has_text="Sat").click()
    dialog.locator(".alarm-schedule-day", has_text="Sun").click()
    page.locator("#securityScheduleSave").click()

    expect(dialog).to_be_hidden()
    row = page.locator("#securitySchedules .automation-summary-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("22:30")
    expect(row).to_contain_text("Perimeter · Every day")
    expect(page.locator("#securitySchedulesCount")).to_contain_text("1 active")
    expect(page.locator("#securityScheduleAdd")).to_be_focused()

    row.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#securityScheduleTime")).to_have_value("22:30")
    expect(page.locator("#securityScheduleAction")).to_have_value("perimeter")


def test_security_schedule_cancel_discards_unsaved_add(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .security-schedules-card > summary").click()
    page.locator("#securityScheduleAdd").click()
    page.locator("#securityScheduleTime").fill("05:45")
    page.keyboard.press("Escape")

    expect(page.locator("#securityScheduleDialog")).to_be_hidden()
    expect(page.locator("#securitySchedules .automation-summary-row")).to_have_count(0)
    expect(page.locator("#securityScheduleAdd")).to_be_focused()


def test_security_tab_adds_scene_pairing_in_editor(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    pairings: List[Dict] = []

    def handle_pairings(route) -> None:
        if route.request.method == "PUT":
            pairings[:] = (route.request.post_data_json or {}).get("entries", [])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"entries": pairings}),
        )

    def handle_cameras(route) -> None:
        if route.request.url.endswith("/presets"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"presets": [{"token": "garden", "name": "Garden"}]}),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"cameras": [{"id": "front-camera", "display_name": "Front camera"}]}),
        )

    page.route("**/api/security/scene-pairings", handle_pairings)
    page.route("**/api/cameras**", handle_cameras)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .scene-pairings-card > summary").click()
    page.locator("#scenePairingAdd").click()

    dialog = page.locator("#scenePairingDialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#scenePairings .automation-summary-row")).to_have_count(0)
    page.locator("#scenePairingZone").select_option("1")
    page.locator("#scenePairingCamera").select_option("front-camera")
    expect(page.locator("#scenePairingPreset option", has_text="Garden")).to_have_count(1)
    page.locator("#scenePairingPreset").select_option("garden")
    page.locator("#scenePairingSave").click()

    expect(dialog).to_be_hidden()
    row = page.locator("#scenePairings .automation-summary-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Front Door")
    expect(row).to_contain_text("Front camera · Garden")
    expect(page.locator("#scenePairingsCount")).to_contain_text("1 active")
    expect(page.locator("#scenePairingAdd")).to_be_focused()

    row.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#scenePairingZone")).to_have_value("1")
    expect(page.locator("#scenePairingCamera")).to_have_value("front-camera")
    expect(page.locator("#scenePairingPreset")).to_have_value("garden")


def test_scene_pairing_cancel_discards_unsaved_add(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/security/scene-pairings",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"entries":[]}'
        ),
    )
    page.route(
        "**/api/cameras**",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"cameras":[{"id":"front-camera","display_name":"Front camera"}]}'
        ),
    )
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .scene-pairings-card > summary").click()
    page.locator("#scenePairingAdd").click()
    page.locator("#scenePairingZone").select_option("1")
    page.keyboard.press("Escape")

    expect(page.locator("#scenePairingDialog")).to_be_hidden()
    expect(page.locator("#scenePairings .automation-summary-row")).to_have_count(0)
    expect(page.locator("#scenePairingAdd")).to_be_focused()


def test_security_tab_adds_override_in_editor(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    overrides: List[Dict] = []

    def handle_overrides(route) -> None:
        if route.request.method == "PUT":
            overrides[:] = (route.request.post_data_json or {}).get("entries", [])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"entries": overrides}),
        )

    page.route("**/api/security/overrides", handle_overrides)
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .security-override-card > summary").click()
    page.locator("#securityOverrideAdd").click()

    dialog = page.locator("#securityOverrideDialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#securityOverrides .automation-summary-row")).to_have_count(0)
    page.locator("#securityOverrideZone").select_option("1")
    page.locator("#securityOverrideRetries").select_option("2")
    page.locator("#securityOverrideSave").click()

    expect(dialog).to_be_hidden()
    row = page.locator("#securityOverrides .automation-summary-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Front Door")
    expect(row).to_contain_text("Bypass after 2 triggers")
    expect(page.locator("#securityOverridesCount")).to_contain_text("1 active")
    expect(page.locator("#securityOverrideAdd")).to_be_focused()

    row.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#securityOverrideZone")).to_have_value("1")
    expect(page.locator("#securityOverrideRetries")).to_have_value("2")


def test_security_override_cancel_discards_unsaved_add(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/security/overrides",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"entries":[]}'
        ),
    )
    _boot(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#paneSecurity .security-override-card > summary").click()
    page.locator("#securityOverrideAdd").click()
    page.locator("#securityOverrideZone").select_option("1")
    page.keyboard.press("Escape")

    expect(page.locator("#securityOverrideDialog")).to_be_hidden()
    expect(page.locator("#securityOverrides .automation-summary-row")).to_have_count(0)
    expect(page.locator("#securityOverrideAdd")).to_be_focused()


def test_alarm_actions_and_weekdays_meet_44px_mobile_target_floor(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)
    page.locator("#tabSecurity").click()

    actions = page.locator("#securityActions .security-action")
    action_boxes = _stable_effective_rects(actions)
    assert len(action_boxes) == 4
    assert all(box.effective.height >= 44 for box in action_boxes)
    # The four actions sit left-to-right with no shared tap zone.
    assert all(
        action_boxes[index].effective.right <= action_boxes[index + 1].effective.left
        for index in range(3)
    )

    page.locator("#paneSecurity .security-schedules-card > summary").click()
    page.locator("#securityScheduleAdd").click()
    days = page.locator(".alarm-schedule-day")
    assert len(_stable_effective_rects(days)) == 7
    assert_min_target(days)
    assert_no_overlap(days)
    assert_no_horizontal_overflow(page)


def test_this_device_presence_is_diagnostic_only(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.add_init_script("""
        localStorage.setItem('home-automation.thisDevicePresence', 'true');
        localStorage.setItem('home-automation.thisDeviceLocation', JSON.stringify({
          lat: 0,
          lon: 0,
          accuracy: 8,
          last_seen: new Date().toISOString(),
        }));
    """)
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _boot(page, base_url)

    page.locator("#tabSecurity").click()

    expect(page.locator("#presenceSummary")).to_have_text("1 home · 1 away · 1 unknown")
    expect(page.locator(".presence-row")).to_have_count(4)
    expect(page.locator(".presence-row").first).to_contain_text("This device")
    expect(page.locator(".presence-row").first).to_contain_text("Browser GPS · diagnostic only")
    expect(page.locator("#presenceRefreshNote")).to_contain_text("not used for alarm automation")
