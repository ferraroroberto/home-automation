"""The unit sheet (#881) — an instant sheet: mode, vanes, rule, schedules, name.

Every change is sent as it is made (decision 4 of #872); the settings rows open
a page of the same sheet, gated on the unit's capabilities.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._geometry import effective_rect


def _open_detail(page: Page, base_url: str, unit_id: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator("#tabAc").click()
    page.wait_for_selector("#acUnits .ac-row", state="visible")
    page.locator(f'#acUnits [data-unit-id="{unit_id}"] .action-row-main').click()
    expect(page.locator("#detailDialog")).to_be_visible()


def _open_page(page: Page, name: str) -> None:
    page.locator(f'#detailDialog [data-page-to="{name}"]').click()
    expect(page.locator(f'#detailDialog .unit-sheet-page[data-page="{name}"]')).to_be_visible()


def test_sheet_shows_mode_as_the_selected_segment_and_both_vanes(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")  # has both vanes
    expect(page.locator("#detailName")).to_have_text("Office")
    expect(page.locator("#detailDialog")).to_have_attribute("data-save-model", "instant")
    expect(page.locator('#detailMode [aria-pressed="true"]')).to_have_attribute("data-value", "Cool")
    expect(page.locator("#detailRoomLine")).to_have_text("Room 22.5° · cooling")
    expect(page.locator("#detailVanesValue")).to_have_text("Auto · Swing")
    _open_page(page, "vanes")
    expect(page.locator("#detailName")).to_have_text("Vanes")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_visible()
    # Back returns to the main page under the unit's name.
    page.locator("#detailBack").click()
    expect(page.locator('#detailDialog .unit-sheet-page[data-page="main"]')).to_be_visible()
    expect(page.locator("#detailName")).to_have_text("Office")
    expect(page.locator("#detailBack")).to_be_hidden()


def test_all_dialog_close_buttons_use_compact_44px_targets(
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
    # + the unit sheet's Back button and the AC schedule editor (#881);
    # the HA capabilities help left the census when #461 made it a folded
    # subsection instead of a modal.
    expect(close_buttons).to_have_count(28)
    expect(page.locator(".detail-close.hit-target")).to_have_count(28)

    target = effective_rect(page.locator("#detailClose"))
    # Exact compact-control contract: 34px visual box, 44px effective hit area.
    assert (target.visual.width, target.visual.height) == (34, 34)
    assert (target.effective.width, target.effective.height) == (44, 44)


def test_offline_unit_disables_unit_commands_only(
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
    _open_page(page, "vanes")
    expect(page.locator("#detailVaneVertical")).to_be_disabled()
    expect(page.locator("#detailVaneHorizontal")).to_be_disabled()
    page.locator("#detailBack").click()
    _open_page(page, "name")
    expect(page.locator("#detailDisplayName")).to_be_enabled()
    page.locator("#detailBack").click()
    _open_page(page, "rule")
    expect(page.locator("#ruleEnabled")).to_be_enabled()


def test_reachable_unit_hides_offline_banner(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailOffline")).to_be_hidden()
    expect(page.locator("#detailMode .segmented-item").first).to_be_enabled()
    expect(page.locator("#detailPower")).to_be_enabled()


def test_vane_rows_gated_on_capability(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    # unit-2: vertical only.
    _open_detail(page, base_url, "unit-2")
    _open_page(page, "vanes")
    expect(page.locator("#detailVaneVerticalRow")).to_be_visible()
    expect(page.locator("#detailVaneHorizontalRow")).to_be_hidden()


def test_no_vane_unit_has_no_vanes_row(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-3")  # no vanes
    expect(page.locator("#detailVanesLink")).to_be_hidden()


def test_vane_change_posts_at_once(
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


def test_mode_segment_posts_at_once(
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


def test_rule_switch_saves_at_once(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailRuleValue")).to_have_text("Off")
    _open_page(page, "rule")
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


def test_name_saves_on_enter(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    _open_page(page, "name")
    page.locator("#detailDisplayName").fill("Fixture den")
    with page.expect_request(
        lambda r: r.url.endswith("/api/units/unit-1/display_name") and r.method == "PUT"
    ) as info:
        page.locator("#detailDisplayName").press("Enter")
    assert info.value.post_data_json == {"display_name": "Fixture den"}
    expect(page.locator('#acUnits [data-unit-id="unit-1"] .action-row-title')).to_have_text("Fixture den")


def test_schedules_add_through_the_staged_editor(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _open_detail(page, base_url, "unit-1")
    expect(page.locator("#detailSchedValue")).to_have_text("None")
    _open_page(page, "schedules")
    expect(page.locator("#schedNote")).to_be_visible()

    # First entry: an On profile, saved through the staged editor.
    page.locator("#schedAdd").click()
    editor = page.locator("#acScheduleDialog")
    expect(editor).to_be_visible()
    expect(editor).to_have_attribute("data-save-model", "staged")
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

    rows = page.locator("#schedList .automation-summary-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(1)).to_contain_text("Off")
    page.locator("#detailBack").click()
    expect(page.locator("#detailSchedValue")).to_have_text("2 on")
