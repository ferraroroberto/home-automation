"""Settings: the inset-group pane, its sheets, and the cards behind its rows.

Settings is inset groups (design.md `settings group`): an overline per group,
one row per setting with its value muted and a chevron, each row opening an
instant sheet that holds what its closed card held before. The flow proves
that shape at phone width, that every row reaches its controls, and then the
cards behind the rows: the boost coordinator's persisted and refused values,
the PV system's rows, validation and horizon editor, and the text-size
control.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings, open_settings_sheet
from tests.e2e._flow import run_flow
from tests.e2e._geometry import assert_no_horizontal_overflow
from tests.e2e._surface import surface

GROUPS = ["Display", "Notifications", "Home & people", "Energy", "Voice", "Diagnostics"]


# The sheets this test is the first (or only) to open; the rest are captured by
# the tests that exercise them.
_SHEET_SURFACES = {
    "notifyAlarmSheet": "settings.notify-alarm-sheet",
    "notifyUpsSheet": "settings.notify-ups-sheet",
    "searxngSheet": "settings.searxng-sheet",
}


def _route_notify_prefs(page: Page) -> None:
    alarm = {"schedule_arm": True, "intrusion": True}
    page.route("**/api/security/notify-prefs", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"prefs": alarm, "telegram_configured": True}),
    ))
    page.route("**/api/ups/notify-prefs", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"prefs": {"power_lost": True}, "telegram_configured": False}),
    ))


def _settings_is_inset_groups_and_every_row_opens_its_sheet(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _route_notify_prefs(page)
    boot_home(page, base_url)
    open_settings(page)

    pane = page.locator("#paneSettings")
    expect(pane.locator(".settings-overline")).to_have_text(GROUPS)
    # No closed cards left: every setting is a row or inline.
    expect(pane.locator("details")).to_have_count(0)

    # Values read without opening anything.
    expect(page.locator("#notifyAlarmValue")).to_have_text("2 of 7 on")
    expect(page.locator("#notifyUpsValue")).to_have_text("Telegram not set up")

    # The two settings changed in place stay inline.
    expect(pane.locator("#textSizeControl")).to_be_visible()
    expect(pane.get_by_role("switch", name="Record navigation log")).to_be_visible()
    surface(page, "settings.root")

    rows = pane.locator("[data-settings-sheet]")
    assert rows.count() == 11
    for index in range(rows.count()):
        row = rows.nth(index)
        sheet_id = row.get_attribute("data-settings-sheet")
        box = row.bounding_box()
        assert box is not None and box["height"] >= 44, (sheet_id, box)
        row.click()
        sheet = page.locator(f"#{sheet_id}")
        expect(sheet).to_be_visible()
        expect(sheet).to_have_attribute("data-save-model", "instant")
        if sheet_id in _SHEET_SURFACES:
            surface(page, _SHEET_SURFACES[sheet_id])
        sheet.locator("[data-sheet-done]").click()
        expect(sheet).to_be_hidden()
        # Focus goes back to the row that opened it.
        expect(row).to_be_focused()


def _boot_boost_card(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, **energy_kwargs,
) -> None:
    mock_api(sample_units)
    mock_energy(**energy_kwargs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    # Settings since #779 (moved off the Energy tab), a sheet since #886.
    open_settings_sheet(page, "boostCoordSheet")


@pytest.mark.chromium_only
def _stored_values_read_in_the_summary_and_populate_the_fields(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={
            "settle_interval_s": 900,
            "admission_margin_w": 250.0,
            "hard_deficit_w": 1500.0,
        },
    )
    # The two knobs that decide how fast the fleet ramps, in the header.
    expect(page.locator("#boostCoordSummary")).to_have_text("15 min · +250 W")
    # Seconds on the wire, minutes on screen — that conversion is the card's.
    expect(page.locator("#boostSettleMin")).to_have_value("15")
    expect(page.locator("#boostHardDeficit")).to_have_value("1500")
    # The floor is served, not hand-copied into the frontend.
    expect(page.locator("#boostSettleMin")).to_have_attribute("min", "5")
    surface(page, "energy.boost-coord-sheet")


@pytest.mark.chromium_only
def _editing_the_settle_interval_and_margin_persists_and_updates_the_summary(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_boost_card(page, base_url, sample_units, mock_api, mock_energy)
    expect(page.locator("#boostCoordSummary")).to_have_text("5 min · +0 W")

    page.locator("#boostSettleMin").fill("12")
    page.locator("#boostSettleMin").press("Tab")

    expect(page.locator("#boostCoordSummary")).to_have_text("12 min · +0 W")
    # Reflected back from the PUT response, i.e. actually persisted.
    expect(page.locator("#boostSettleMin")).to_have_value("12")

    page.locator("#boostAdmissionMargin").fill("300")
    page.locator("#boostAdmissionMargin").press("Tab")

    expect(page.locator("#boostCoordSummary")).to_have_text("12 min · +300 W")


@pytest.mark.chromium_only
def _out_of_range_values_are_refused_and_rolled_back(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """The settle floor is physical — the solar meter only publishes every 5
    minutes — so a shorter interval, like a negative margin, must explain
    itself, not be silently clamped."""
    _boot_boost_card(
        page, base_url, sample_units, mock_api, mock_energy,
        boost_coord={"settle_interval_s": 600, "admission_margin_w": 100.0},
    )

    page.locator("#boostSettleMin").fill("2")
    page.locator("#boostSettleMin").press("Tab")

    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("at least 5 min")
    # Rolled back to what is stored — the card never shows an unsaved value.
    expect(page.locator("#boostSettleMin")).to_have_value("10")
    expect(page.locator("#boostCoordSummary")).to_have_text("10 min · +100 W")

    page.locator("#boostAdmissionMargin").fill("-50")
    page.locator("#boostAdmissionMargin").press("Tab")

    expect(toast).to_contain_text("0 W or more")
    expect(page.locator("#boostAdmissionMargin")).to_have_value("100")


def _boot_pv_system(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, **energy_kwargs,
) -> None:
    mock_api(sample_units)
    mock_energy(**energy_kwargs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    # Visit Energy first: its entry loads the forecast card whose params line
    # the editor's saves are checked against. The editor itself moved off the
    # Energy tab into Settings in #779.
    page.locator("#tabEnergy").click()
    page.wait_for_selector("#paneEnergy", state="visible")
    open_settings_sheet(page, "pvSystemSheet")  # a Settings sheet since #886


@pytest.mark.chromium_only
def _card_renders_a_summary_row_per_panel_row(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[
            {"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0},
            {"kwp": 0.9, "tilt_deg": 15.0, "azimuth_deg": 180.0},
        ],
    )

    rows = page.locator("#pvArrayList .action-row")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("7.9 kWp · 15° · S")
    expect(rows.first).to_contain_text("facing south")
    expect(rows.nth(1)).to_contain_text("0.9 kWp · 15° · N")
    # The header carries the system total so the card reads without expanding.
    expect(page.locator("#pvSystemTotal")).to_have_text("8.8 kWp")
    surface(page, "energy.pv-system-sheet")


@pytest.mark.chromium_only
def _empty_config_shows_the_empty_state_not_a_blank_list(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(page, base_url, sample_units, mock_api, mock_energy, pv_arrays=[])

    expect(page.locator("#pvArrayList .empty-state")).to_be_visible()
    expect(page.locator("#pvArrayList")).to_contain_text("No panel rows yet")
    expect(page.locator("#pvArrayAdd")).to_be_visible()
    surface(page, "energy.pv-system-empty")


@pytest.mark.chromium_only
def _adding_a_row_validates_then_updates_the_forecast_and_its_toast(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """One editor session: an invalid tilt is refused against its field, the
    corrected row saves and reaches the forecast, and a later save with no
    estimate available degrades the toast instead of reading undefined/NaN."""
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )
    rows = page.locator("#pvArrayList .action-row")
    dialog = page.locator("#pvArrayDialog")
    toast = page.locator("#toast")
    expect(page.locator("#forecastParams")).to_have_text("7.9 kWp · 15° · S · PR 0.80")

    # A negative tilt is the mistake the azimuth convention invites, so it
    # must explain itself rather than be silently clamped to 0.
    page.locator("#pvArrayAdd").click()
    expect(dialog).to_be_visible()
    page.locator("#pvArrayKwp").fill("1")
    page.locator("#pvArrayTilt").fill("-15")
    page.locator("#pvArraySave").click()

    error = page.locator("#pvArrayTiltError")
    expect(error).to_be_visible()
    expect(error).to_contain_text("between 0 and 90")
    expect(page.locator("#pvArrayTilt")).to_have_attribute("aria-invalid", "true")
    surface(page, "energy.pv-array-dialog")
    # Still open, still one row — nothing was persisted.
    expect(dialog).to_be_visible()
    expect(rows).to_have_count(1)

    page.locator("#pvArrayKwp").fill("0.9")
    page.locator("#pvArrayTilt").fill("15")
    page.locator("#pvArrayAzimuth").fill("180")
    # The convention hint echoes what was typed, in words.
    expect(page.locator("#pvArrayAzimuthEcho")).to_have_text("facing north")
    page.locator("#pvArraySave").click()

    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(2)
    # The whole point: the forecast is now computed from the edited array.
    expect(page.locator("#forecastParams")).to_have_text(
        "7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80"
    )
    # Issue #564: the save toast carries that same recomputed estimate rather
    # than firing before the forecast refetch lands (mock_energy's forecast
    # fixture fixes expected_total_kwh at 12.3, so this is deterministic).
    expect(toast).to_have_text("PV system saved · today's estimate 12.3 kWh")

    # Issue #564: a forecast that comes back unavailable just drops the suffix.
    page.route(
        "**/api/energy/forecast*",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": false, "reason": "no_config"}',
        ),
    )
    page.locator("#pvArrayAdd").click()
    page.locator("#pvArrayKwp").fill("0.9")
    page.locator("#pvArrayTilt").fill("15")
    page.locator("#pvArrayAzimuth").fill("180")
    page.locator("#pvArraySave").click()

    expect(dialog).to_be_hidden()
    expect(toast).to_have_text("PV system saved")


@pytest.mark.chromium_only
def _horizon_point_editor_opens_from_the_sheet(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    _boot_pv_system(
        page, base_url, sample_units, mock_api, mock_energy,
        pv_arrays=[{"kwp": 7.9, "tilt_deg": 15.0, "azimuth_deg": 0.0}],
    )
    page.locator("#pvHorizonAdd").click()
    expect(page.locator("#pvHorizonDialog")).to_be_visible()
    expect(page.locator("#pvHorizonEditorTitle")).to_have_text("Add horizon point")
    surface(page, "energy.pv-horizon-dialog")


# design.md text-size: 93.75% / 100% / 112.5% of the 16px UA root.
_ROOT_PX = {"small": 15.0, "default": 16.0, "large": 18.0}


_STORAGE_KEY = "home-automation.textsize"


def _root_px(page: Page) -> float:
    return float(
        page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    )


def _boot_settings(page: Page, base_url: str, mocks: List[Callable]) -> None:
    for install in mocks:
        install()
    boot_home(page, base_url)
    open_settings(page)


@pytest.mark.chromium_only
def _each_step_changes_root_font_size_and_persists_across_reload(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    _boot_settings(
        page, base_url,
        [lambda: mock_api(sample_units), mock_energy, mock_security, mock_presence],
    )
    control = page.locator("#textSizeControl")
    expect(control).to_be_visible()
    # Nothing stored yet: the Default step is the pressed one.
    assert page.evaluate("document.documentElement.dataset.textsize") == "default"
    assert _root_px(page) == _ROOT_PX["default"]
    expect(control.locator('[data-textsize="default"]')).to_have_attribute(
        "aria-pressed", "true"
    )

    seen = {}
    for step in ("small", "large", "default"):
        control.locator(f'[data-textsize="{step}"]').click()
        expect(control.locator(f'[data-textsize="{step}"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        seen[step] = _root_px(page)
        assert seen[step] == _ROOT_PX[step], f"{step}: root font-size {seen[step]}px"
        assert page.evaluate("k => localStorage.getItem(k)", _STORAGE_KEY) == step
    assert seen["small"] < seen["default"] < seen["large"]

    # Persistence: Large survives a reload, and is on the root before any module
    # has run (the boot script, not the binding, is what stamps it).
    control.locator('[data-textsize="large"]').click()
    page.reload(wait_until="domcontentloaded")
    assert page.evaluate("document.documentElement.dataset.textsize") == "large"
    assert _root_px(page) == _ROOT_PX["large"]
    page.wait_for_selector("#paneHome", state="visible")
    open_settings(page)
    expect(page.locator('#textSizeControl [data-textsize="large"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def _large_text_does_not_overflow_the_phone_viewport(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script(
        f"try {{ localStorage.setItem('{_STORAGE_KEY}', 'large'); }} catch (e) {{}}"
    )
    _boot_settings(
        page, base_url,
        [lambda: mock_api(sample_units), mock_energy, mock_security, mock_presence],
    )
    assert _root_px(page) == _ROOT_PX["large"]
    assert_no_horizontal_overflow(page)
    control = page.locator("#textSizeControl")
    box = control.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390


def test_settings(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """Settings: inset groups and every row's sheet, boost coordinator card, PV system card, text size."""
    run_flow(
        request, page, base_url,
        _settings_is_inset_groups_and_every_row_opens_its_sheet,
        _stored_values_read_in_the_summary_and_populate_the_fields,
        _editing_the_settle_interval_and_margin_persists_and_updates_the_summary,
        _out_of_range_values_are_refused_and_rolled_back,
        _card_renders_a_summary_row_per_panel_row,
        _empty_config_shows_the_empty_state_not_a_blank_list,
        _adding_a_row_validates_then_updates_the_forecast_and_its_toast,
        _horizon_point_editor_opens_from_the_sheet,
        _each_step_changes_root_font_size_and_persists_across_reload,
        _large_text_does_not_overflow_the_phone_viewport,
    )
