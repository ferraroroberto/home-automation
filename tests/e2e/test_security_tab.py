"""Security tab — alarm status plus its schedules, scene pairings, overrides.

The pane's own loading/unavailable/stale states, the three automation editors
reachable from it, and the mobile tap-target floor for the alarm actions and
weekday chips. Cameras and presence — rendered in the same pane but separate
features — live in `test_cameras.py` and `test_presence.py`.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Locator, Page, expect

from tests.e2e._app import boot_home
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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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


def test_security_tab_adds_alarm_schedule(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)

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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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
    boot_home(page, base_url)
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

    page.locator("#paneSecurity .security-schedules-card > summary").click()
    page.locator("#securityScheduleAdd").click()
    days = page.locator(".alarm-schedule-day")
    assert len(_stable_effective_rects(days)) == 7
    assert_min_target(days)
    assert_no_overlap(days)
    assert_no_horizontal_overflow(page)
