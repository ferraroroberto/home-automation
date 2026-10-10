"""Settings pane on the shared system (#886, Step 8/8 of #872).

Settings is inset groups (design.md `settings group`): an overline per group,
one row per setting with its value muted and a chevron, each row opening an
instant sheet that holds what its closed card held before. This proves the
shape at phone width and that every row still reaches its controls.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings

GROUPS = ["Display", "Notifications", "Home & people", "Energy", "Voice", "Diagnostics"]


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


def test_settings_is_inset_groups_and_every_row_opens_its_sheet(
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
        sheet.locator("[data-sheet-done]").click()
        expect(sheet).to_be_hidden()
        # Focus goes back to the row that opened it.
        expect(row).to_be_focused()
