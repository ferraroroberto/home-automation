"""Blind schedule editor in the Devices tab's Blinds card (issue #871)."""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect

from tests.e2e._geometry import assert_no_horizontal_overflow

_BLIND = {
    "has_switch": False, "has_cover": True, "metered": False, "category": "qt",
    "has_valid_ip": True, "reachable": True, "switch_on": None, "power_w": None,
    "current_ma": None, "voltage_v": None, "energy_kwh": None, "error": None,
}


def _stub_schedules(page: Page, entries: List[Dict]) -> List[Dict]:
    """Stub GET/PUT /api/blinds/schedules; returns the list of PUT bodies."""
    puts: List[Dict] = []
    store = {"entries": entries}

    def handle(route: Route) -> None:
        if route.request.method == "PUT":
            body = route.request.post_data_json
            puts.append(body)
            store["entries"] = body["entries"]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"enabled": True, "count": len(store["entries"]),
                                       "entries": store["entries"]}))

    page.route("**/api/blinds/schedules", handle)
    return puts


def _boot(page: Page, base_url: str, sample_units, mock_api, mock_energy, mock_tuya,
          entries: List[Dict]) -> List[Dict]:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_tuya([
        {**_BLIND, "device_id": "blind-a", "name": "Test Blind A"},
        {**_BLIND, "device_id": "blind-b", "name": "Test Blind B"},
    ])
    puts = _stub_schedules(page, entries)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.locator("#blindsCard").evaluate("el => { el.open = true; }")
    return puts


def test_blind_schedule_lists_and_adds_an_entry(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    puts = _boot(page, base_url, sample_units, mock_api, mock_energy, mock_tuya, [
        {"id": "down", "enabled": True, "time": "21:30", "action": "close",
         "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "targets": [],
         "presence": "any"},
    ])

    # The existing entry renders as a summary row under the blind rows, with
    # the group row and per-blind buttons still on the card.
    rows = page.locator("#blindSchedules .automation-summary-row")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".automation-summary-title")).to_have_text("21:30")
    expect(rows.first.locator(".automation-summary-meta")).to_have_text("Down · Every day · All blinds")
    expect(page.locator("#blindsAllUp")).to_be_visible()
    expect(page.locator('[data-device-id="blind-a"] .blind-btn')).to_have_count(3)
    assert_no_horizontal_overflow(page)

    # Add a weekend "up" for one blind, only when someone is home.
    page.locator("#blindScheduleAdd").click()
    dialog = page.locator("#blindScheduleDialog")
    expect(dialog).to_be_visible()
    page.locator("#blindScheduleTime").fill("07:00")
    page.locator("#blindScheduleAction").select_option("open")
    page.locator("#blindSchedulePresence").select_option("home")
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        dialog.locator("#blindScheduleDays button", has_text=day).click()
    dialog.locator("#blindScheduleTargets button", has_text="Test Blind B").click()
    expect(dialog.locator("#blindScheduleTargets button", has_text="All blinds")).to_have_attribute(
        "aria-pressed", "false"
    )
    assert_no_horizontal_overflow(page)
    page.locator("#blindScheduleSave").click()

    expect(rows).to_have_count(2)
    assert len(puts) == 1
    added = next(e for e in puts[0]["entries"] if e["time"] == "07:00")
    assert added["action"] == "open"
    assert added["days"] == ["sat", "sun"]
    assert added["targets"] == ["blind-b"]
    assert added["presence"] == "home"
    expect(rows.first.locator(".automation-summary-meta")).to_have_text(
        "Up · Weekends · Someone home · Test Blind B"
    )
