"""Tab navigation + the new Home summary and Energy dashboard.

Covers the Home / AC / Energy switcher, the read-only AC summary on Home, and
an Energy-tab render (the live flow row + charts) against stubbed energy
fixtures — on both the Chromium-desktop and WebKit/iPhone projections.
"""

from __future__ import annotations

import copy
import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


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

    entry = page.locator(".alarm-schedule-entry")
    expect(entry).to_have_count(1)
    entry.locator(".alarm-schedule-time").fill("22:30")
    entry.locator(".alarm-schedule-action").select_option("perimeter")
    entry.locator(".alarm-schedule-day", has_text="Sat").click()
    entry.locator(".alarm-schedule-day", has_text="Sun").click()

    expect(entry.locator(".alarm-schedule-action")).to_have_value("perimeter")
    expect(page.locator("#securitySchedulesCount")).to_contain_text("1 active")


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
