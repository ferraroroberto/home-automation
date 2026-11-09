"""The AC tab: pane states, the row's controls and the unit sheet.

Pane states are the `data-state` machine (loading, true-empty, unavailable,
stale) and the cached-snapshot paint. The row draws a unit on the shared row
(power is its switch); the setpoint and fan live in the unit sheet, which
sends each change as it is made (decision 4 of #872) through POST
/api/units/{id}, the stub echoing the merged snapshot. The sheet's settings
rows open a page of the same sheet, gated on the unit's capabilities.
"""

from __future__ import annotations

import copy
import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, hold_reads
from tests.e2e._flow import run_flow
from tests.e2e._geometry import assert_no_horizontal_overflow, effective_rect
from tests.e2e._surface import surface


@pytest.mark.chromium_only
def _ac_tab_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, mock_energy: Callable,
) -> None:
    mock_energy()
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"units": []}',
        ),
    )
    release = hold_reads(page, "/api/units")
    boot_home(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "loading")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "Reading AC units…"
    )
    surface(page, "ac.loading")
    release()
    expect(page.locator("#paneAc")).to_have_attribute("data-state", "empty")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "No AC units configured"
    )
    surface(page, "ac.empty")


@pytest.mark.chromium_only
def _ac_tab_shows_contextual_unavailable_state(
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
    boot_home(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "error")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "AC units unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("melcloud.example.internal")
    surface(page, "ac.unavailable")


@pytest.mark.chromium_only
def _ac_poll_failure_preserves_units_and_disables_actions(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    page.locator("#tabAc").click()
    expect(page.locator("#acUnits .ac-row")).to_have_count(len(sample_units))
    surface(page, "ac.units")

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
    expect(page.locator("#acUnits .ac-row")).to_have_count(len(sample_units))
    expect(page.locator("#acUnits .ac-line-toggle:enabled")).to_have_count(0)
    expect(page.locator("#acSummary .ac-line-toggle:enabled")).to_have_count(0)
    expect(page.locator("#acFeedback")).not_to_contain_text("melcloud.example.internal")
    surface(page, "ac.stale")


@pytest.mark.chromium_only
def _units_snapshot_paints_before_live_refresh(
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
    boot_home(page, base_url)

    # #522: no per-card pill — the cached-vs-live swap is communicated once,
    # via the existing pane-level thin stale-note line (renderAcFeedback()).
    expect(page.locator("#acSummary")).to_contain_text("Snapshot Office")
    expect(page.locator("#acFeedback")).to_contain_text("Last saved")

    expect(page.locator("#acSummary")).to_contain_text("Live Office", timeout=4000)
    expect(page.locator("#acSummary")).not_to_contain_text("Snapshot Office")
    expect(page.locator("#acFeedback")).not_to_contain_text("Last saved")


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Home's boot read already drew the AC rows, so they are visible the moment
    # the tab opens; the tab's own read then redraws them. Wait for that read
    # and its render, or a measurement can land on a row being replaced and
    # read a detached node's all-zero rect (the intermittent red seen in #906).
    with page.expect_response("**/api/units") as units:
        page.locator("#tabAc").click()
    units.value.finished()
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
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


def _power_toggle_posts_and_rerenders(
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


def _setpoint_taps_settle_into_one_post(
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


def _closing_the_sheet_sends_a_dialled_setpoint(
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


def _setpoint_stops_at_the_mode_minimum(
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


def _fan_segment_posts_fan_speed(
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


def _offline_unit_row_is_marked_and_inert(
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
    surface(page, "ac.offline")


def _offline_unit_row_marked_on_home_summary(
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
def _row_meta_carries_the_rule_and_the_boost_chip(
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


def _unit_row_target_is_44px_and_clear_of_its_switch(
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
def _on_switch_track_is_the_accent_not_green(
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


def _open_detail(page: Page, base_url: str, unit_id: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabAc").click()
    page.wait_for_selector("#acUnits .ac-row", state="visible")
    page.locator(f'#acUnits [data-unit-id="{unit_id}"] .action-row-main').click()
    expect(page.locator("#detailDialog")).to_be_visible()


def _open_page(page: Page, name: str) -> None:
    page.locator(f'#detailDialog [data-page-to="{name}"]').click()
    expect(page.locator(f'#detailDialog .unit-sheet-page[data-page="{name}"]')).to_be_visible()


def _sheet_shows_mode_as_the_selected_segment_and_both_vanes(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")  # has both vanes
    expect(page.locator("#detailName")).to_have_text("Office")
    expect(page.locator("#detailDialog")).to_have_attribute("data-save-model", "instant")
    expect(page.locator('#detailMode [aria-pressed="true"]')).to_have_attribute("data-value", "Cool")
    expect(page.locator("#detailRoomLine")).to_have_text("Room 22.5° · cooling")
    expect(page.locator("#detailVanesValue")).to_have_text("Auto · Swing")
    surface(page, "ac.unit-sheet")
    _open_page(page, "vanes")
    expect(page.locator("#detailName")).to_have_text("Vanes")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_visible()
    surface(page, "ac.unit-sheet-vanes")
    # Back returns to the main page under the unit's name.
    page.locator("#detailBack").click()
    expect(page.locator('#detailDialog .unit-sheet-page[data-page="main"]')).to_be_visible()
    expect(page.locator("#detailName")).to_have_text("Office")
    expect(page.locator("#detailBack")).to_be_hidden()


def _all_dialog_close_buttons_use_compact_44px_targets(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")

    close_buttons = page.locator(".detail-close")
    # 15 + the two presence dialogs (#438) + the network device-group rename
    # dialog (#513) + the reminder editor dialog (#314) + the Wi-Fi walk-test
    # device picker (#547) + the PV-array editor dialog (#pvArrayDialog, #561)
    # + the horizon-point editor dialog (#pvHorizonDialog, #578 part b)
    # + the circuit rename/sign-flip dialog (#circuitDialog, #25)
    # + the iCloud trust-renewal code dialog (#presenceTrustDialog, #659)
    # + the camera preset-name prompt dialog (#cameraPresetNameDialog, #722)
    # + the blind schedule editor dialog (#blindScheduleDialog, #871)
    # + the unit sheet's Back button and the AC schedule editor (#881)
    # + the Security tab's Schedules, Scene capture, Override and Accounts
    # sheets (#882) + the Energy tab's All figures sheet (#883) + the Devices
    # tab's UPS, light, blind schedule, voice satellites, recent interactions
    # and Network sheets (#884) + Home's wake alarm editor and timer sheet
    # (#885) + the eleven Settings sheets (#886) + the Car history sheet
    # (#1121); the HA capabilities help left the census when #461 made it a
    # folded subsection instead of a modal.
    expect(close_buttons).to_have_count(53)
    # Every close is the vendored modal's icon-button (#906), which owns the
    # invisible 44px target.
    expect(page.locator(".detail-close.icon-button")).to_have_count(53)
    # And every title takes focus on open (vendored modal contract), so the
    # close never opens ringed and no field pops the phone keyboard unasked.
    expect(page.locator('.detail-header h2[tabindex="-1"][autofocus]')).to_have_count(52)

    target = effective_rect(page.locator("#detailClose"))
    # Exact compact-control contract: 34px visual box, 44px effective hit area.
    assert (target.visual.width, target.visual.height) == (34, 34)
    assert (target.effective.width, target.effective.height) == (44, 44)


def _offline_unit_disables_unit_commands_only(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    """Offline (#520): every control that writes to the unit goes inert and the
    banner explains why, but the settings stored server-side (name, rule,
    schedules) stay editable."""
    sample_units[0]["reachable"] = False  # unit-1, has both vanes
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")

    expect(page.locator("#detailOffline")).to_be_visible()
    expect(page.locator("#detailRoomLine")).to_have_text("Last read 22.5°")
    expect(page.locator("#detailPower")).to_be_disabled()
    expect(page.locator("#detailTempDown")).to_be_disabled()
    expect(page.locator("#detailTempUp")).to_be_disabled()
    expect(page.locator("#detailMode .segmented-item:enabled")).to_have_count(0)
    expect(page.locator("#detailFanSpeed .segmented-item:enabled")).to_have_count(0)
    surface(page, "ac.unit-sheet-offline")
    _open_page(page, "vanes")
    expect(page.locator("#detailVaneVertical")).to_be_disabled()
    expect(page.locator("#detailVaneHorizontal")).to_be_disabled()
    page.locator("#detailBack").click()
    _open_page(page, "name")
    expect(page.locator("#detailDisplayName")).to_be_enabled()
    page.locator("#detailBack").click()
    _open_page(page, "rule")
    expect(page.locator("#ruleEnabled")).to_be_enabled()


def _reachable_unit_hides_offline_banner(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailOffline")).to_be_hidden()
    expect(page.locator("#detailMode .segmented-item").first).to_be_enabled()
    expect(page.locator("#detailPower")).to_be_enabled()


def _vane_rows_gated_on_capability(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    # unit-2: vertical only.
    _open_detail(page, base_url, "unit-2")
    _open_page(page, "vanes")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_hidden()


def _no_vane_unit_has_no_vanes_row(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-3")  # no vanes
    expect(page.locator("#detailVanesLink")).to_be_hidden()


def _vane_change_posts_at_once(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    _open_page(page, "vanes")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator("#detailVaneVertical").select_option("Swing")
    assert info.value.post_data_json == {"vane_vertical_direction": "Swing"}


def _mode_segment_posts_at_once(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1") and r.method == "POST"
    ) as info:
        page.locator('#detailMode [data-value="Heat"]').click()
    assert info.value.post_data_json == {"operation_mode": "Heat"}
    expect(page.locator('#detailMode [aria-pressed="true"]')).to_have_attribute("data-value", "Heat")
    # The row's avatar follows the mode.
    expect(page.locator('#acUnits [data-unit-id="unit-1"] .row-avatar use')).to_have_attribute(
        "href", "#i-flame"
    )


def _rule_switch_saves_at_once(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailRuleValue")).to_have_text("Off")
    _open_page(page, "rule")
    surface(page, "ac.unit-sheet-rule")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1/rule") and r.method == "PUT"
    ) as info:
        page.locator("#ruleEnabled").click()
    assert info.value.post_data_json["enabled"] is True
    # A number saves on change (blur).
    page.locator("#ruleCoolTarget").fill("23")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1/rule") and r.method == "PUT"
    ) as info:
        page.locator("#ruleCoolTarget").press("Tab")
    assert info.value.post_data_json["cool_target"] == 23
    page.locator("#detailBack").click()
    expect(page.locator("#detailRuleValue")).to_have_text("Cool 23°")


def _name_saves_on_enter(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    _open_page(page, "name")
    surface(page, "ac.unit-sheet-name")
    page.locator("#detailDisplayName").fill("Fixture den")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1/display_name") and r.method == "PUT"
    ) as info:
        page.locator("#detailDisplayName").press("Enter")
    assert info.value.post_data_json == {"display_name": "Fixture den"}
    expect(page.locator('#acUnits [data-unit-id="unit-1"] .action-row-title')).to_have_text("Fixture den")


def _schedules_add_through_the_staged_editor(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailSchedValue")).to_have_text("None")
    _open_page(page, "schedules")
    expect(page.locator("#schedNote")).to_be_visible()
    surface(page, "ac.unit-sheet-schedules")

    # First entry: an On profile, saved through the staged editor.
    page.locator("#schedAdd").click()
    editor = page.locator("#acScheduleDialog")
    expect(editor).to_be_visible()
    expect(editor).to_have_attribute("data-save-model", "staged")
    surface(page, "ac.ac-schedule-dialog")
    page.locator("#acScheduleTime").fill("07:30")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1/schedule") and r.method == "PUT"
    ) as info:
        page.locator("#acScheduleSave").click()
    entries = info.value.post_data_json["entries"]
    assert [e["time"] for e in entries] == ["07:30"]
    assert entries[0]["power"] is True and entries[0]["operation_mode"] == "Cool"
    expect(editor).to_be_hidden()

    # Second entry: Off only powers down, so its profile hides.
    page.locator("#schedAdd").click()
    page.locator("#acScheduleTime").fill("23:00")
    page.locator("#acSchedulePower").select_option("false")
    expect(page.locator("#acScheduleProfile")).to_be_hidden()
    page.locator("#acScheduleSave").click()

    rows = page.locator("#schedList .action-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(1)).to_contain_text("Off")
    page.locator("#detailBack").click()
    expect(page.locator("#detailSchedValue")).to_have_text("2 on")


def test_ac_tab(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """AC tab: pane states, row controls (power, setpoint, fan, offline, rule chip), and the unit sheet (mode, vanes, gating, schedules, name)."""
    run_flow(
        request, page, base_url,
        _ac_tab_distinguishes_loading_from_true_empty,
        _ac_tab_shows_contextual_unavailable_state,
        _ac_poll_failure_preserves_units_and_disables_actions,
        _units_snapshot_paints_before_live_refresh,
        _power_toggle_posts_and_rerenders,
        _setpoint_taps_settle_into_one_post,
        _closing_the_sheet_sends_a_dialled_setpoint,
        _setpoint_stops_at_the_mode_minimum,
        _fan_segment_posts_fan_speed,
        _offline_unit_row_is_marked_and_inert,
        _offline_unit_row_marked_on_home_summary,
        _row_meta_carries_the_rule_and_the_boost_chip,
        _unit_row_target_is_44px_and_clear_of_its_switch,
        (_on_switch_track_is_the_accent_not_green, {'theme': 'light'}),
        (_on_switch_track_is_the_accent_not_green, {'theme': 'dark'}),
        _sheet_shows_mode_as_the_selected_segment_and_both_vanes,
        _all_dialog_close_buttons_use_compact_44px_targets,
        _offline_unit_disables_unit_commands_only,
        _reachable_unit_hides_offline_banner,
        _vane_rows_gated_on_capability,
        _no_vane_unit_has_no_vanes_row,
        _vane_change_posts_at_once,
        _mode_segment_posts_at_once,
        _rule_switch_saves_at_once,
        _name_saves_on_enter,
        _schedules_add_through_the_staged_editor,
    )
