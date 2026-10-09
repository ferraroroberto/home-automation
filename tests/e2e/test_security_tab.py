"""Security tab — the glance card, detectors, recent events and automations.

The pane's own loading/unavailable/stale states, the glance card (mode,
trouble, next schedule), the Detectors group, Recent events, the three
automation editors reachable from their sheets, and the mobile tap-target
floor for the alarm actions and weekday chips. Cameras and presence — rendered in the same pane but separate
features — live in `test_cameras.py` and `test_presence.py`.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e._app import boot_home, hold_reads
from tests.e2e._geometry import (
    EffectiveRect,
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    effective_rects,
)


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
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "error")
    expect(page.locator("#securityFeedback .empty-state-message")).to_have_text(
        "Security unavailable"
    )
    expect(page.locator("#securityState")).to_be_hidden()
    expect(page.locator("#toast")).not_to_contain_text("risco.example.internal")


def test_security_tab_loads_then_keeps_state_and_disables_actions_on_poll_failure(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    release = hold_reads(page, "/api/security")
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "loading")
    expect(page.locator("#securityFeedback .empty-state-message")).to_have_text(
        "Reading security status…"
    )
    release()
    expect(page.locator("#paneSecurity")).to_have_attribute("data-state", "ready")
    expect(page.locator("#securityState")).to_contain_text("Not armed")
    expect(page.locator("#homeSecurityState")).to_contain_text("Not armed")

    page.route(
        "**/api/security",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"risco.example.internal timed out after 10 seconds"}',
        ),
    )
    # Re-entering the tab is what re-reads it, as the first visit did.
    page.locator("#tabHome").click()
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


def test_security_schedule_editor_cancels_then_adds(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """Escape discards an unsaved add (at phone width, where the editor is a
    compact modal), then a real add saves, lists, and reopens with its values."""
    projection_viewport = page.viewport_size
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)

    page.locator("#tabSecurity").click()
    page.locator("#securitySchedulesOpen").click()
    expect(page.locator("#securitySchedulesSheet")).to_have_attribute("data-save-model", "instant")
    dialog = page.locator("#securityScheduleDialog")
    rows = page.locator("#securitySchedules .automation-summary-row")

    page.locator("#securityScheduleAdd").click()
    page.locator("#securityScheduleTime").fill("05:45")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(0)
    expect(page.locator("#securityScheduleAdd")).to_be_focused()

    page.set_viewport_size(projection_viewport)
    page.locator("#securityScheduleAdd").click()
    expect(dialog).to_be_visible()
    expect(rows).to_have_count(0)
    page.locator("#securityScheduleTime").fill("22:30")
    page.locator("#securityScheduleAction").select_option("perimeter")
    dialog.locator(".alarm-schedule-day", has_text="Sat").click()
    dialog.locator(".alarm-schedule-day", has_text="Sun").click()
    page.locator("#securityScheduleSave").click()

    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("22:30")
    expect(rows).to_contain_text("Perimeter · Every day")
    expect(page.locator("#securitySchedulesCount")).to_contain_text("1 active")
    expect(page.locator("#securityScheduleAdd")).to_be_focused()

    rows.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#securityScheduleTime")).to_have_value("22:30")
    expect(page.locator("#securityScheduleAction")).to_have_value("perimeter")


def test_scene_pairing_editor_cancels_then_adds(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """Escape discards an unsaved pairing, then a real add saves (camera and
    preset pickers fed by the stubs), lists, and reopens with its values."""
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
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#scenePairingsOpen").click()
    dialog = page.locator("#scenePairingDialog")
    rows = page.locator("#scenePairings .automation-summary-row")

    page.locator("#scenePairingAdd").click()
    page.locator("#scenePairingZone").select_option("1")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(0)
    expect(page.locator("#scenePairingAdd")).to_be_focused()

    page.locator("#scenePairingAdd").click()
    expect(dialog).to_be_visible()
    expect(rows).to_have_count(0)
    page.locator("#scenePairingZone").select_option("1")
    page.locator("#scenePairingCamera").select_option("front-camera")
    expect(page.locator("#scenePairingPreset option", has_text="Garden")).to_have_count(1)
    page.locator("#scenePairingPreset").select_option("garden")
    page.locator("#scenePairingSave").click()

    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("Front Door")
    expect(rows).to_contain_text("Front camera · Garden")
    expect(page.locator("#scenePairingsCount")).to_contain_text("1 active")
    expect(page.locator("#scenePairingAdd")).to_be_focused()

    rows.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#scenePairingZone")).to_have_value("1")
    expect(page.locator("#scenePairingCamera")).to_have_value("front-camera")
    expect(page.locator("#scenePairingPreset")).to_have_value("garden")


def test_security_override_editor_cancels_then_adds(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """Escape discards an unsaved override, then a real add saves, lists, and
    reopens with its values."""
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
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#securityOverridesOpen").click()
    dialog = page.locator("#securityOverrideDialog")
    rows = page.locator("#securityOverrides .automation-summary-row")

    page.locator("#securityOverrideAdd").click()
    page.locator("#securityOverrideZone").select_option("1")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(0)
    expect(page.locator("#securityOverrideAdd")).to_be_focused()

    page.locator("#securityOverrideAdd").click()
    expect(dialog).to_be_visible()
    expect(rows).to_have_count(0)
    page.locator("#securityOverrideZone").select_option("1")
    page.locator("#securityOverrideRetries").select_option("2")
    page.locator("#securityOverrideSave").click()

    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("Front Door")
    expect(rows).to_contain_text("Bypass after 2 triggers")
    expect(page.locator("#securityOverridesCount")).to_contain_text("1 active")
    expect(page.locator("#securityOverrideAdd")).to_be_focused()

    rows.locator(".automation-summary-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#securityOverrideZone")).to_have_value("1")
    expect(page.locator("#securityOverrideRetries")).to_have_value("2")


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
    boot_home(page, base_url)
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

    page.locator("#securitySchedulesOpen").click()
    page.locator("#securityScheduleAdd").click()
    days = page.locator(".alarm-schedule-day")
    assert len(_stable_effective_rects(days)) == 7
    assert_min_target(days)
    assert_no_overlap(days)
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
def test_recent_events_show_three_and_all_events_opens_the_alarm_log(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """Decision 8 of #872 (#882): the last three events as time-led rows
    (sentence case, who as words), and All events opens the activity log
    filtered to the alarm, handing focus back when it closes."""
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/security/events**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"events": [
                {"time": "2026-06-22T10:00:00+00:00", "name": "SYSTEM ARMED", "user_id": 3},
                {"time": "2026-06-22T09:00:00+00:00", "name": "Zone opened", "user_id": 0},
                {"time": "2026-06-22T08:00:00+00:00", "name": "System disarmed", "user_id": 1},
                {"time": "2026-06-22T07:00:00+00:00", "name": "Older event", "user_id": 0},
            ]}),
        ),
    )
    activity_urls: List[str] = []

    def handle_activity(route) -> None:
        if "/api/activity/domains" in route.request.url:
            body = {"domains": ["hvac", "security"]}
        else:
            activity_urls.append(route.request.url)
            body = {"events": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/activity**", handle_activity)
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    rows = page.locator("#securityEvents .security-event")
    expect(rows).to_have_count(3)
    expect(rows.locator(".action-row-title")).to_have_text(
        ["System armed", "Zone opened", "System disarmed"]
    )
    expect(rows.first.locator(".row-time")).to_be_visible()
    expect(rows.first.locator(".action-row-meta")).to_contain_text("User 3")
    # No actor, no badge (#362).
    expect(rows.nth(1)).not_to_contain_text("User")

    page.locator("#securityEventsAll").click()
    expect(page.locator("#activityDialog")).to_be_visible()
    expect(page.locator("#activityDomain")).to_have_value("security")
    expect(page.locator("#activityNote")).to_be_visible()
    assert any("domain=security" in url for url in activity_urls), activity_urls
    page.keyboard.press("Escape")
    expect(page.locator("#activityDialog")).to_be_hidden()
    expect(page.locator("#securityEventsAll")).to_be_focused()
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
def test_glance_card_shows_next_schedule_and_trouble_opens_the_detector(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """#882: the glance card answers "is the house protected?" without a tap:
    the next schedule as one line, and a trouble chip that opens the one
    troubled detector's sheet."""
    mock_api(sample_units)
    mock_energy()
    mock_presence()
    zone = {"type": 1, "status": "closed", "active": False, "triggered": False,
            "bypassed": False, "display_name": None, "hidden": False,
            "trouble_ignored": False}
    mock_security({
        "reachable": True, "label": "Disarmed", "mode": "disarmed",
        "supported_actions": ["disarm", "partial", "perimeter", "arm"],
        "ac_lost": False, "assumed_control_panel_state": False,
        "zones": [
            {**zone, "id": 1, "name": "Back Door", "trouble": False},
            {**zone, "id": 2, "name": "Garage", "trouble": True},
        ],
    })
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    page.route(
        "**/api/security/schedules",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"entries": [
                {"id": "s1", "enabled": True, "time": "23:59", "days": every_day, "action": "arm"},
                {"id": "s2", "enabled": False, "time": "00:00", "days": every_day, "action": "disarm"},
            ]}),
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    next_line = page.locator("#securityNext")
    expect(next_line).to_contain_text("Next: Arm full")
    expect(next_line).to_contain_text("at 23:59")
    expect(page.locator("#securitySchedulesCount")).to_have_text("1 active")

    chip = page.locator("#securityState button.security-trouble-badge")
    expect(chip).to_have_text("1 trouble")
    expect(chip).to_have_attribute("data-tone", "attention")
    # The Home card shows the same chip, but as a plain status chip.
    expect(page.locator("#homeSecurityState button")).to_have_count(0)
    chip.click()
    expect(page.locator("#zoneDialog")).to_be_visible()
    expect(page.locator("#zoneDetailName")).to_have_text("Garage")


@pytest.mark.chromium_only
def test_detectors_fold_behind_show_all_and_filter_when_long(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """#882: a long detector list shows its first five (a detector that needs
    you first, the rest A-Z), "Show all" unfolds it, and past twelve a filter
    narrows it (design.md action-row: long lists get a filter)."""
    mock_api(sample_units)
    mock_energy()
    mock_presence()
    zone = {"type": 1, "status": "closed", "active": False, "triggered": False,
            "display_name": None, "hidden": False, "trouble_ignored": False}
    zones = [
        {**zone, "id": n, "name": f"Zone {n:02d}", "bypassed": n == 3, "trouble": n == 9}
        for n in range(1, 15)
    ]
    mock_security({
        "reachable": True, "label": "Disarmed", "mode": "disarmed",
        "supported_actions": ["disarm", "partial", "perimeter", "arm"],
        "ac_lost": False, "assumed_control_panel_state": False, "zones": zones,
    })
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    rows = page.locator("#securityZones .security-zone")
    titles = rows.locator(".action-row-title")
    expect(titles).to_have_text(["Zone 09", "Zone 01", "Zone 02", "Zone 03", "Zone 04"])
    expect(rows.first.locator('.chip[data-tone="attention"]')).to_have_text("Trouble")
    expect(page.locator("#securityZonesMeta")).to_have_text("14 · 1 bypassed")
    more = page.locator("#securityZonesMore")
    expect(more).to_have_text("Show all 14")

    more.click()
    expect(rows).to_have_count(14)
    expect(more).to_have_text("Show fewer")

    page.locator("#securityZoneFilter").fill("zone 1")
    expect(titles).to_have_text(["Zone 10", "Zone 11", "Zone 12", "Zone 13", "Zone 14"])
    expect(more).to_be_hidden()
    page.locator("#securityZoneFilter").fill("nothing like it")
    expect(rows).to_have_count(0)
    expect(page.locator("#securityZonesNote")).to_have_text("No detector matches.")
    assert_no_horizontal_overflow(page)


@pytest.mark.chromium_only
def test_alarm_mode_is_the_selected_segment_and_armed_is_not_red(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """#879 (decision 3 of #872): the mode is the selected segment of one
    control on both tabs, an armed alarm is a normal state (plain text, the
    raised segment), trouble is an attention chip, and a detector that is
    simply active carries no chip at all. #888: the control has no outer
    border, the selected segment sits on the card surface, and every label
    (selected, available, unavailable) keeps one text colour."""
    mock_api(sample_units)
    mock_energy()
    mock_presence()
    zone = {"type": 1, "status": "closed", "active": False, "triggered": False,
            "display_name": None, "hidden": False}
    mock_security({
        "reachable": True, "label": "Armed", "mode": "armed",
        "supported_actions": ["disarm", "partial", "perimeter", "arm"],
        "ac_lost": False, "assumed_control_panel_state": False,
        "zones": [
            {**zone, "id": 1, "name": "Back Door", "bypassed": False, "trouble": False},
            {**zone, "id": 2, "name": "Garage", "bypassed": True, "trouble": True},
        ],
    })
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    for pane in ("#securityActions", "#homeSecurityActions"):
        expect(page.locator(f"{pane} .security-action")).to_have_text(
            ["Off", "Partial", "Perimeter", "Full"]
        )
        expect(page.locator(f'{pane} [aria-pressed="true"]')).to_have_text("Full")
        control = page.locator(pane)
        assert control.evaluate(
            "el => ['Top', 'Right', 'Bottom', 'Left'].map("
            "s => getComputedStyle(el)['border' + s + 'Width'])"
        ) == ["0px"] * 4
        card_bg = control.evaluate("el => getComputedStyle(el.closest('.card')).backgroundColor")
        assert page.locator(f'{pane} [aria-pressed="true"]').evaluate(
            "el => getComputedStyle(el).backgroundColor"
        ) == card_bg
        colors = page.locator(f"{pane} .security-action").evaluate_all(
            "els => els.map(el => getComputedStyle(el).color)"
        )
        assert len(set(colors)) == 1, colors
    # Off is the one way out of an armed mode; the other modes wait for it.
    expect(page.locator("#securityActions .security-action:enabled")).to_have_text(["Off"])

    word = page.locator("#securityState .security-state-word")
    expect(word).to_have_text("Fully armed")
    line_color = page.locator("#securityState").evaluate("el => getComputedStyle(el).color")
    assert word.evaluate("el => getComputedStyle(el).color") == line_color
    expect(page.locator("#securityState .security-trouble-badge")).to_have_attribute(
        "data-tone", "attention"
    )

    # A detector that needs you sorts first (#882).
    rows = page.locator("#securityZones .security-zone")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".chip")).to_have_text(["Bypassed", "Trouble"])
    expect(rows.nth(0).locator('.chip[data-tone="attention"]')).to_have_text("Trouble")
    expect(rows.nth(1).locator(".chip")).to_have_count(0)
