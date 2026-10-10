"""Home tab: a pure dashboard (#885, Step 7/8 of #872).

The page header with the weather as its live line, then three cards: the
House glance card (the arm control, the compact energy flow, who is home and
any exception chip), the Climate group (the AC rows) and Next up (wake alarms,
timers and reminders as time-led rows with their editors). Everything Home
used to show that has another home (the plug totals, the UPS tile, the
family locator, Home Assistant and voice) is checked gone from Home here and
covered where it lives now.

Every API Home reads is stubbed: no test reaches the alarm panel, the UPS, a
cloud or the real wake-alarm and reminder stores.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from pathlib import Path
from typing import Callable, Dict, List, Optional

from playwright.sync_api import Page, Route, expect

from tests.e2e._app import boot_home
from tests.e2e._geometry import (
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    effective_rects,
)

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def _stub_weather(page: Page) -> None:
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
                "forecast_code": 61,
                "temp_min_c": 18,
                "temp_max_c": 27,
            }),
        ),
    )


def _stub_ups(page: Page, mains_online: bool = True) -> None:
    ups = {
        "available": True, "source": "nut",
        "status": "online" if mains_online else "on_battery",
        "mains_online": mains_online, "battery_charge_pct": 90,
        "runtime_seconds": 3600, "alarms": [],
    }
    page.route("**/api/ups", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"ups": ups})))


def _stub_next_up(
    page: Page,
    alarms: Optional[List[Dict]] = None,
    timers: Optional[List[Dict]] = None,
    reminders: Optional[List[Dict]] = None,
) -> Dict[str, List]:
    """In-memory wake alarms, timers and reminders; returns the writes seen."""
    store = {"alarms": list(alarms or []), "timers": list(timers or []),
             "reminders": list(reminders or [])}
    writes: Dict[str, List] = {"alarms": [], "timer_starts": [], "timer_cancels": [], "reminders": []}

    def alarms_route(route: Route) -> None:
        req = route.request
        if req.method == "PUT":
            store["alarms"] = req.post_data_json["entries"]
            writes["alarms"].append(store["alarms"])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"entries": store["alarms"]}))

    def timers_route(route: Route) -> None:
        req = route.request
        if req.method == "POST":
            writes["timer_starts"].append(req.post_data_json)
        elif req.method == "DELETE":
            timer_id = req.url.rsplit("/", 1)[-1]
            writes["timer_cancels"].append(timer_id)
            store["timers"] = [t for t in store["timers"] if t["id"] != timer_id]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"timers": store["timers"]}))

    def reminders_route(route: Route) -> None:
        req = route.request
        if req.method == "PUT":
            store["reminders"] = req.post_data_json["entries"]
            writes["reminders"].append(store["reminders"])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"entries": store["reminders"]}))

    page.route("**/api/wake-alarms", alarms_route)
    page.route("**/api/wake-timers", timers_route)
    page.route("**/api/wake-timers/*", timers_route)
    page.route("**/api/reminders", reminders_route)
    return writes


def _put(path: str) -> Callable:
    return lambda response: response.url.endswith(path) and response.request.method == "PUT"


def _boot(page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
          mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
          security: Optional[Dict] = None, mains_online: bool = True) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security(security)
    mock_presence()
    _stub_weather(page)
    _stub_ups(page, mains_online)
    boot_home(page, base_url)


def test_home_header_controls_have_non_overlapping_44px_targets(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    _stub_weather(page)
    boot_home(page, base_url)
    # The weather is the header's live line (decision 2 of #872): the sky and
    # the temperature now, then today's range; the forecast sky and the place
    # are in its accessible name.
    line = page.locator('#paneHome .page-head .status[data-head="home"]')
    expect(line).to_have_text("Clear 24° · 18° / 27°")
    expect(line).to_have_attribute("aria-label", re.compile(r"at Home: .*today rain"))

    # The theme toggle + Settings gear moved from the weather tile into the
    # page header in #779; they keep the compact 34px + .hit-target recipe.
    buttons = page.locator("#paneHome .page-head .home-toggle")
    targets = effective_rects(buttons)
    assert len(targets) == 2
    for target in targets:
        assert (target.visual.width, target.visual.height) == (34, 34)
    # They are .icon-button's (project-scaffolding#339): a glyph on nothing at rest.
    for paint in buttons.evaluate_all("els => els.map(el => { const s = getComputedStyle(el); return [s.backgroundColor, s.borderTopWidth]; })"):
        assert paint == ["rgba(0, 0, 0, 0)", "0px"]
    assert_min_target(buttons)
    assert_no_overlap(buttons)
    # The two compact controls sit left-to-right with no shared tap zone.
    assert targets[0].effective.right <= targets[1].effective.left
    assert_no_horizontal_overflow(page)


def test_home_is_a_dashboard_of_house_climate_and_next_up(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _stub_next_up(page)
    _boot(page, base_url, sample_units, mock_api, mock_energy, mock_security, mock_presence)

    # Three cards under the header, and nothing that has a home elsewhere.
    cards = page.locator("#paneHome > .card")
    expect(cards).to_have_count(4)
    assert cards.evaluate_all("els => els.map(el => el.id || el.className.split(' ')[1])") == [
        "home-head", "houseCard", "acSummary", "nextUpCard",
    ]

    house = page.locator("#houseCard")
    # The arm control is the Security tab's, its mode the selected segment.
    expect(house.locator(".security-action")).to_have_text(["Off", "Partial", "Perimeter", "Full"])
    expect(house.locator('.security-action[aria-pressed="true"]')).to_have_text("Off")
    expect(page.locator("#homeSecurityState")).to_have_text("Alarm state: Not armed")
    expect(page.locator("#homeEnergyFlow")).to_be_visible()
    # A normal house shows no exception chip.
    expect(page.locator("#houseChips .house-chip")).to_have_count(0)
    people = page.locator("#housePeople .house-people-row")
    expect(people.locator(".action-row-title")).to_have_text("1 home · 1 away · 1 unknown")
    expect(people.locator(".action-row-meta-text")).to_contain_text("Away Phone away · 1.1 km")

    expect(page.locator("#climateMeta")).to_have_text(re.compile(r"^\d+ running$"))
    expect(page.locator("#nextUpNote")).to_have_text(
        "Nothing coming up. Add an alarm, a timer or a reminder."
    )
    assert_no_horizontal_overflow(page)

    # Who is home opens Security › People, where each person's sheet is.
    people.locator(".action-row-main").click()
    expect(page.locator("#paneSecurity")).to_be_visible()
    expect(page.locator("#presenceCard")).to_be_in_viewport()


def test_house_chips_open_the_tab_that_owns_the_exception(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    _stub_next_up(page)
    zone = {"type": 1, "status": "closed", "active": False, "triggered": False,
            "display_name": None, "hidden": False}
    _boot(
        page, base_url, sample_units, mock_api, mock_energy, mock_security, mock_presence,
        security={
            "reachable": True, "label": "Disarmed", "mode": "disarmed",
            "supported_actions": ["disarm", "partial", "perimeter", "arm"],
            "ac_lost": False, "assumed_control_panel_state": False,
            "zones": [{**zone, "id": 1, "name": "Garage", "bypass": False, "trouble": True}],
        },
        mains_online=False,
    )

    chips = page.locator("#houseChips .house-chip")
    expect(chips).to_have_count(2)
    trouble = chips.filter(has_text="1 trouble")
    battery = chips.filter(has_text="On battery")
    expect(trouble).to_have_attribute("data-tone", "attention")
    expect(battery).to_have_attribute("data-tone", "attention")

    # Detector trouble opens the detector on the Security tab.
    trouble.click()
    expect(page.locator("#paneSecurity")).to_be_visible()
    expect(page.locator("#zoneDialog")).to_be_visible()
    expect(page.locator("#zoneDetailName")).to_have_text("Garage")
    page.keyboard.press("Escape")

    # The UPS on battery opens the UPS sheet on Devices.
    page.locator("#tabHome").click()
    battery.click()
    expect(page.locator("#paneIot")).to_be_visible()
    expect(page.locator("#upsSheet")).to_be_visible()
    expect(page.locator("#upsSheetStatus")).to_contain_text("On battery")


def test_home_shows_ac_summary_line_per_unit(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    lines = page.locator("#acSummary .ac-row")
    expect(lines).to_have_count(len(sample_units))
    # One scannable line per unit: name + an actionable power toggle (issue #72).
    expect(page.locator("#acSummary")).to_contain_text("Office")
    expect(page.locator("#acSummary .ac-line-toggle")).to_have_count(len(sample_units))
    # The shared row (#880): tapping the row, not its switch, opens the unit's
    # sheet, the same one the AC tab opens.
    office = lines.filter(has_text="Office").locator(".action-row-main")
    office.click()
    expect(page.locator("#detailDialog")).to_have_attribute("open", "")
    page.keyboard.press("Escape")
    expect(page.locator("#detailDialog")).not_to_have_attribute("open", "")


def test_wake_alarm_editor_is_staged(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """A wake alarm row opens a staged editor (#885): × discards, Save writes
    once; it replaced inline cards that saved on every keystroke."""
    writes = _stub_next_up(page, alarms=[{
        "id": "a1", "label": "Wake up", "enabled": True, "time": "07:00",
        "days": _WEEKDAYS, "date": None, "ringing": False,
    }])
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    row = page.locator('#wakeAlarmsList [data-alarm-id="a1"]')
    expect(row.locator(".row-time")).to_have_text("07:00")
    expect(row.locator(".action-row-title")).to_have_text("Wake up")
    expect(row.locator(".action-row-meta-text")).to_have_text("Weekdays")
    expect(row.locator(".toggle")).to_have_attribute("aria-checked", "true")

    dialog = page.locator("#wakeAlarmDialog")
    row.locator(".action-row-main").click()
    expect(dialog).to_be_visible()
    expect(page.locator("#wakeAlarmEditorTitle")).to_have_text("Edit wake alarm")
    page.locator("#wakeAlarmTime").fill("06:30")
    page.locator("#wakeAlarmEditorClose").click()
    expect(dialog).to_be_hidden()
    expect(row.locator(".row-time")).to_have_text("07:00")
    assert writes["alarms"] == []

    row.locator(".action-row-main").click()
    page.locator("#wakeAlarmTime").fill("06:30")
    page.locator("#wakeAlarmDays button", has_text="Sat").click()
    page.locator("#wakeAlarmSave").click()
    expect(dialog).to_be_hidden()
    expect(row.locator(".row-time")).to_have_text("06:30")
    expect(row.locator(".action-row-meta-text")).to_have_text("Mon, Tue, Wed, Thu, Fri, Sat")
    assert len(writes["alarms"]) == 1
    assert writes["alarms"][0][0]["time"] == "06:30"

    # The row's switch saves at once, like every row switch.
    with page.expect_response(_put("/api/wake-alarms")):
        row.locator(".toggle").click()
    expect(row.locator(".toggle")).to_have_attribute("aria-checked", "false")
    assert writes["alarms"][-1][0]["enabled"] is False

    page.locator("#wakeAlarmAdd").click()
    expect(page.locator("#wakeAlarmEditorTitle")).to_have_text("Add wake alarm")


def test_timers_and_reminders_are_next_up_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    today = time.strftime("%Y-%m-%d")
    writes = _stub_next_up(
        page,
        timers=[{"id": "t1", "label": "", "seconds": 600, "ends_at": time.time() + 300, "ringing": False}],
        reminders=[{"id": "r1", "text": "Take the bins out", "done": False,
                    "date": today, "time": "20:00", "created_at": ""}],
    )
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    timer = page.locator('#wakeTimersList [data-timer-id="t1"]')
    expect(timer.locator(".action-row-title")).to_have_text("Timer")
    expect(timer.locator(".row-time")).to_have_text(re.compile(r"^[45]:\d\d$"))
    timer.locator(".wake-timer-cancel").click()
    expect(timer).to_have_count(0)
    assert writes["timer_cancels"] == ["t1"]

    page.locator("#wakeTimerOpen").click()
    expect(page.locator("#wakeTimerSheet")).to_be_visible()
    with page.expect_response(lambda r: "/api/wake-timers" in r.url and r.request.method == "POST"):
        page.locator('.wake-timer-presets [data-seconds="300"]').click()
    expect(page.locator("#wakeTimerSheet")).to_be_hidden()
    assert writes["timer_starts"] == [{"seconds": 300, "label": ""}]

    reminder = page.locator('#remindersList [data-reminder-id="r1"]')
    expect(reminder.locator(".row-time")).to_have_text("20:00")
    expect(reminder.locator(".action-row-title")).to_have_text("Take the bins out")
    expect(reminder.locator(".action-row-meta-text")).to_have_text("Today")
    with page.expect_response(_put("/api/reminders")):
        reminder.locator(".toggle").click()
    expect(reminder.locator(".action-row-meta-text")).to_have_text("Done · Today")
    assert writes["reminders"][-1][0]["done"] is True
    reminder.locator(".action-row-main").click()
    expect(page.locator("#reminderDialog")).to_be_visible()
    expect(page.locator("#reminderText")).to_have_value("Take the bins out")


def test_perf_review_ready_selector_is_visible_on_home(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """/perf-review scores a cold launch as ready when `.fleet.toml`'s selector
    shows; one that never shows on Home leaves the check unmeasured (#777)."""
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    selector = _perf_review_ready_selector()
    expect(page.locator(selector).first).to_be_visible()


def _perf_review_ready_selector() -> str:
    fleet = Path(__file__).resolve().parents[2] / ".fleet.toml"
    return tomllib.loads(fleet.read_text(encoding="utf-8"))["perf"]["review"]["ready_selector"]
