"""Devices tab: the Plugs and Blinds groups, the Power card, switch + covers.

Drives the Devices tab against stubbed local-Tuya fixtures (no LAN, no cloud)
on both the Chromium-desktop and WebKit/iPhone projections. Plugs and blinds
are open groups on the shared row (#884): these tests cover a metered plug's
watts, a switch round-trip, the blind segmented verb, the Power glance card,
and the Offline row the unreachable plugs fold into.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, NamedTuple

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import hold_reads
from tests.e2e._geometry import assert_no_horizontal_overflow, effective_rects


def _boot_plugs(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    # Stub units + energy too so boot doesn't touch the real cloud, then open
    # the Plugs tab (lazy-loads GET /api/tuya on entry).
    mock_api(sample_units)
    mock_energy()
    mock_tuya(sample_plugs)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")


# Device rows (not the Offline fold row) in the Plugs and Blinds groups.
_DEVICE_ROWS = "#plugsList .action-row[data-device-id], #blindsList .action-row[data-device-id]"


def test_plugs_tab_renders_all_devices(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # One row per reachable device, split across the two groups; the offline
    # plug folds into the Offline row until that row is opened (#884).
    expect(page.locator(_DEVICE_ROWS)).to_have_count(len(sample_plugs) - 1)
    expect(page.locator("#plugsList")).to_contain_text("Test Heater")
    expect(page.locator("#blindsList")).to_contain_text("Test Blind")
    expect(page.locator("#plugsList .plugs-offline-row")).to_contain_text("One plug not reachable")


class _FeedbackPanel(NamedTuple):
    endpoint: str
    empty_body: Dict
    tab: str  # tab to open after boot
    feedback: str
    message: str  # the element inside `feedback` that carries the words
    loading_message: str
    empty_message: str
    error_message: str
    leaked_host: str  # named in the 503 detail; must never reach a toast


# The Plugs panel and the Devices tab's UPS row (Home's UPS tile until #885)
# render the same feedback contract, so the loading and unavailable states run
# as one parametrized test each. The stale-on-refresh case stays per panel: the
# refresh trigger and the last-good content it must preserve differ between
# them.
_FEEDBACK_PANELS = [
    pytest.param(_FeedbackPanel(
        "/api/tuya", {"devices": []}, "#tabIot", "#plugsFeedback", ".empty-state-message",
        "Reading plugs and blinds…", "No Smart Life devices configured",
        "Plugs and blinds unavailable", "192.0.2.60",
    ), id="plugs"),
    pytest.param(_FeedbackPanel(
        "/api/ups", {"ups": {"available": False, "source": "none", "error": None}}, "#tabIot",
        "#upsRows .ups-row", ".action-row-meta-text",
        "Reading UPS status…", "No UPS detected",
        "Status unavailable", "192.0.2.70",
    ), id="ups"),
]

def _open_panel(page: Page, base_url: str, panel: _FeedbackPanel) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator(panel.tab).click()


@pytest.mark.parametrize("panel", _FEEDBACK_PANELS)
def test_panel_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    panel: _FeedbackPanel,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    page.route(
        f"**{panel.endpoint}",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(panel.empty_body),
        ),
    )
    release = hold_reads(page, panel.endpoint)
    _open_panel(page, base_url, panel)

    feedback = page.locator(panel.feedback)
    message = page.locator(f"{panel.feedback} {panel.message}")
    expect(feedback).to_have_attribute("data-state", "loading")
    expect(message).to_have_text(panel.loading_message)
    release()
    expect(feedback).to_have_attribute("data-state", "empty")
    expect(message).to_have_text(panel.empty_message)


@pytest.mark.parametrize("panel", _FEEDBACK_PANELS)
def test_panel_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    panel: _FeedbackPanel,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    page.route(
        f"**{panel.endpoint}",
        lambda route: route.fulfill(
            status=503, content_type="application/json",
            body=json.dumps({"detail": f"{panel.leaked_host} timed out after 10 seconds"}),
        ),
    )
    _open_panel(page, base_url, panel)

    expect(page.locator(panel.feedback)).to_have_attribute("data-state", "error")
    expect(page.locator(f"{panel.feedback} {panel.message}")).to_have_text(panel.error_message)
    expect(page.locator("#toast")).not_to_contain_text(panel.leaked_host)


def test_plug_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(
        page, base_url, sample_units, sample_plugs,
        mock_api, mock_energy, mock_tuya,
    )
    expect(page.locator(_DEVICE_ROWS)).to_have_count(len(sample_plugs) - 1)

    page.route(
        "**/api/tuya",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"device 192.0.2.60 timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabHome").click()
    page.locator("#tabIot").click()

    expect(page.locator("#plugsFeedback")).to_have_attribute("data-state", "stale")
    expect(page.locator(_DEVICE_ROWS)).to_have_count(len(sample_plugs) - 1)
    expect(page.locator("#plugsFeedback")).to_contain_text("Last updated")
    expect(page.locator("#plugsFeedback")).to_contain_text("live data unavailable")
    expect(page.locator("#plugsFeedback")).not_to_contain_text("192.0.2.60")


def test_ups_poll_failure_preserves_last_good_status(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    failing = {"value": False}
    ups = {
        "available": True,
        "source": "nut",
        "status": "online",
        "mains_online": True,
        "battery_charge_pct": 90,
        "runtime_seconds": 3600,
        "alarms": [],
    }

    def handle_ups(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"nut host 192.0.2.70 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ups": ups}),
        )

    page.route("**/api/ups", handle_ups)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    row = page.locator("#upsRows .ups-row")
    expect(row).to_have_attribute("data-state", "ready")
    expect(row).to_contain_text("90%")

    failing["value"] = True
    page.locator("#tabAc").click()
    page.locator("#tabIot").click()

    # The row keeps the last reading; the sheet says how old it is (#884).
    expect(row).to_have_attribute("data-state", "stale")
    expect(row).to_contain_text("90%")
    row.locator(".action-row-main").click()
    status = page.locator("#upsSheetStatus")
    expect(status).to_contain_text("90%")
    expect(status).to_contain_text("Last updated")
    expect(status).to_contain_text("live data unavailable")
    expect(page.locator("#upsSheet")).not_to_contain_text("192.0.2.70")


def test_ups_snapshot_paints_before_live_refresh(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    # #522: the UPS used to pop a per-card orange pill for this cached-
    # while-loading window; it must now use the same thin `.ups-stale-note`
    # line the poll-failure case already renders (test above), not just when
    # upsView.state === 'stale' but also while a snapshot is painted ahead of
    # the first live fetch resolving. Home's UPS tile is gone (#885), so the
    # note is checked in its one home, the UPS sheet on Devices.
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    cached_ups = {
        "available": True, "source": "nut", "status": "online",
        "mains_online": True, "battery_charge_pct": 77,
        "runtime_seconds": 1800, "alarms": [],
    }
    live_ups = {
        "available": True, "source": "nut", "status": "online",
        "mains_online": True, "battery_charge_pct": 90,
        "runtime_seconds": 3600, "alarms": [],
    }
    snapshot_store = {
        "version": 1,
        "snapshots": {
            "ups": {
                "saved_at": "2026-06-24T20:15:00.000Z",
                "body": {"ups": cached_ups},
            },
        },
    }
    page.add_init_script("""
        localStorage.setItem('home-automation.apiSnapshots.v1', JSON.stringify(%s));
    """ % json.dumps(snapshot_store))
    # The live read is held until released, so the cached-snapshot window is
    # observable without a timer (#798, as #789 did for the loading tests).
    page.route(
        "**/api/ups",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ups": live_ups}),
        ),
    )
    release = hold_reads(page, "/api/ups")
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    page.locator("#upsRows .ups-row .action-row-main").click()

    status = page.locator("#upsSheetStatus")
    expect(status).to_contain_text("77%")
    expect(status.locator(".ups-stale-note")).to_contain_text("Last saved")

    release()
    expect(status).to_contain_text("90%")
    expect(status.locator(".ups-stale-note")).to_have_count(0)


def test_metered_plug_shows_watts(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Wattage is the metered plug row's meta line while it is on. Grouped
    # digits per the shared fmtW (format.js, #383) — one watt format across tabs.
    watts = page.locator('[data-device-id="plug-1"] .action-row-meta-text')
    expect(watts).to_be_visible()
    expect(watts).to_have_text("1,450 W")


def test_plugs_stats_block_summarizes(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # The Power glance card (#884): Heater on, Lamp off; the live watts are the
    # metered, reachable plug's, and it is named as the biggest draw.
    expect(page.locator("#powerNow")).to_be_visible()
    expect(page.locator("#powerOnCount")).to_have_text("1 on")
    expect(page.locator("#powerWatts")).to_have_text("1,450 W")
    expect(page.locator("#powerTop")).to_have_text("Test Heater 1450")
    expect(page.locator('#paneIot .status[data-head="iot"]')).to_have_text("1 on · 1,450 W")


def test_plug_rename_round_trips(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Tap the row → rename dialog opens; saving relabels the row from the override.
    page.locator('[data-device-id="plug-1"] .action-row-main').click()
    expect(page.locator("#plugDialog")).to_be_visible()
    field = page.locator("#plugDisplayName")
    field.fill("Garage Heater")
    field.press("Enter")  # Enter saves → PUT /api/tuya/{id}/display_name
    expect(page.locator('[data-device-id="plug-1"] .action-row-title')).to_have_text("Garage Heater")


def test_switch_toggle_round_trips(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # Test Lamp starts OFF; clicking its toggle flips it ON via the read-back.
    toggle = page.locator('[data-device-id="plug-2"] .toggle')
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()
    expect(page.locator('[data-device-id="plug-2"] .toggle')).to_have_attribute(
        "aria-checked", "true"
    )


def test_blind_has_labelled_controls(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # The blind's one trailing item is the Up · Stop · Down segmented verb
    # (decision 6 of #872): glyphs, each named in words for assistive tech.
    buttons = page.locator('[data-device-id="cover-1"] .blind-btn')
    expect(buttons).to_have_count(3)
    for button, word in zip(buttons.all(), ["Up", "Stop", "Down"]):
        expect(button).to_have_attribute("aria-label", f"{word} Test Blind")
        expect(button).to_have_attribute("title", word)
    boxes = effective_rects(buttons)
    assert all(box.effective.height >= 44 and box.visual.width >= 44 for box in boxes)
    # The three buttons sit left-to-right with no shared tap zone.
    assert all(
        boxes[index].effective.right <= boxes[index + 1].effective.left
        for index in range(2)
    )
    assert_no_horizontal_overflow(page)
    # Up is actionable and does not raise (stub acks the action).
    page.locator('[data-device-id="cover-1"] .blind-btn[data-action="open"]').click()


def _with_second_blind_and_light(sample_plugs: List[Dict]) -> List[Dict]:
    """sample_plugs plus a second blind and a Tuya light (#181)."""
    blank = {
        "has_switch": False, "has_cover": False, "metered": False,
        "has_valid_ip": True, "reachable": True, "switch_on": None,
        "power_w": None, "current_ma": None, "voltage_v": None,
        "energy_kwh": None, "error": None,
    }
    return sample_plugs + [
        {**blank, "device_id": "cover-2", "name": "Test Blind Two",
         "category": "qt", "has_cover": True},
        {**blank, "device_id": "light-1", "name": "Test Dimmer",
         "category": "dj", "has_switch": True, "is_light": True, "switch_on": False,
         "has_brightness": True, "brightness_pct": 40},
    ]


def test_blinds_group_buttons_move_every_listed_blind(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _boot_plugs(
        page, base_url, sample_units, _with_second_blind_and_light(sample_plugs),
        mock_api, mock_energy, mock_tuya,
    )

    # The house-wide move is the first row's segmented verb.
    group = page.locator("#blindsCard .blinds-all-row .segmented-item")
    for button, label in zip(group.all(), ["All up", "All stop", "All down"]):
        expect(button).to_have_attribute("aria-label", label)
    expect(page.locator("#blindsAllMeta")).to_have_text("2 blinds")
    assert all(box.effective.height >= 44 for box in effective_rects(group))
    assert_no_horizontal_overflow(page)

    # One request carries every blind the card lists, in parallel server-side.
    with page.expect_request("**/api/tuya/covers") as request:
        page.locator("#blindsAllDown").click()
    body = request.value.post_data_json
    assert body["action"] == "close"
    assert sorted(body["device_ids"]) == ["cover-1", "cover-2"]
    expect(page.locator("#toast")).to_contain_text("All blinds down")

    # Stop is never held behind a move still in flight (#899): an unresponsive
    # blind keeps All up's request open, and Stop must go out regardless.
    held: List = []

    def _hold_up(route) -> None:
        if (route.request.post_data_json or {}).get("action") == "open":
            held.append(route)
        else:
            route.fallback()

    page.route("**/api/tuya/covers", _hold_up)
    with page.expect_request("**/api/tuya/covers"):
        page.locator("#blindsAllUp").click()
    with page.expect_request("**/api/tuya/covers") as stop:
        page.locator("#blindsAllStop").click()
    assert stop.value.post_data_json["action"] == "stop"
    expect(page.locator("#toast")).to_contain_text("All blinds stopped")
    assert len(held) == 1  # All up is still unanswered
    # The late answer to the earlier All up doesn't overwrite Stop's toast.
    held[0].fulfill(status=200, content_type="application/json",
                    body='{"action": "open", "results": [], "failed": 0}')
    page.wait_for_timeout(300)
    expect(page.locator("#toast")).to_contain_text("All blinds stopped")


@pytest.mark.chromium_only
def test_tuya_light_lists_under_lights_not_plugs(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(
        page, base_url, sample_units, _with_second_blind_and_light(sample_plugs),
        mock_api, mock_energy, mock_tuya,
    )

    # The dimmer is a light (#181): a row in the Lights group, not the Plugs
    # one, and it counts toward neither the Plugs count nor the Power card.
    expect(page.locator('#lightsList [data-device-id="light-1"]')).to_be_visible()
    expect(page.locator('#plugsList [data-device-id="light-1"]')).to_have_count(0)
    expect(page.locator("#plugsCount")).to_have_text("3")
    expect(page.locator("#powerOnCount")).to_have_text("1 on")

    # Its toggle rides the plug switch path and re-renders in the Lights card.
    toggle = page.locator('#lightsList [data-device-id="light-1"] .toggle')
    expect(toggle).to_have_attribute("aria-checked", "false")
    with page.expect_request("**/api/tuya/light-1/switch"):
        toggle.click()
    expect(page.locator('#lightsList [data-device-id="light-1"] .toggle')).to_have_attribute(
        "aria-checked", "true"
    )


def test_offline_device_unavailable_without_blocking_others(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)

    # The unreachable plug folds into the Offline row (#884); the reachable
    # plug keeps its switch.
    expect(page.locator('[data-device-id="plug-3"]')).to_have_count(0)
    expect(page.locator('[data-device-id="plug-1"] .toggle')).to_be_visible()
    fold = page.get_by_test_id("plugs-offline-toggle")
    expect(fold).to_have_attribute("aria-expanded", "false")

    # Opening the row lists it: no switch, a plain reason, never its error.
    fold.click()
    offline = page.locator('[data-device-id="plug-3"]')
    expect(offline).to_have_class(re.compile(r"\bis-unavailable\b"))
    expect(offline.locator(".action-row-meta-text")).to_have_text("Not reachable right now")
    expect(offline.locator(".toggle")).to_have_count(0)
    expect(offline).not_to_contain_text("devices.json")
    expect(page.get_by_test_id("plugs-offline-toggle")).to_have_attribute("aria-expanded", "true")

    # The choice persists across a reload.
    page.reload(wait_until="domcontentloaded")
    page.locator("#tabIot").click()
    expect(page.locator('[data-device-id="plug-3"]')).to_be_visible()


def test_no_ip_adapters_fold_with_the_offline_plugs(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs_with_no_ip: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    """A no-IP adapter is one more unreachable plug: the Offline row replaced
    the Reachable only toggle (#884), and the setup hint stays off the row."""
    _boot_plugs(
        page, base_url, sample_units, sample_plugs_with_no_ip,
        mock_api, mock_energy, mock_tuya,
    )

    expect(page.locator(_DEVICE_ROWS)).to_have_count(3)
    expect(page.locator("#plugsList .plugs-offline-row")).to_contain_text("2 plugs not reachable")
    page.get_by_test_id("plugs-offline-toggle").click()
    expect(page.locator(_DEVICE_ROWS)).to_have_count(5)
    no_ip = page.locator('[data-device-id="plug-noip"]')
    expect(no_ip).to_contain_text("Not reachable right now")
    expect(no_ip).not_to_contain_text("tinytuya")


def test_add_device_is_gated_by_a_confirm(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    """#612: the cloud sync must never fire on a stray tap.

    Cancelling has to leave the account untouched, so the request is asserted
    to happen only after an explicit confirm.
    """
    _boot_plugs(page, base_url, sample_units, sample_plugs, mock_api, mock_energy, mock_tuya)
    calls: List[str] = []
    page.on("request", lambda req: calls.append(req.url) if "/api/tuya/pair" in req.url else None)

    page.get_by_test_id("plugs-pair").click()
    expect(page.locator("#confirmDialog")).to_be_visible()
    page.locator("#confirmCancel").click()
    expect(page.locator("#confirmDialog")).to_be_hidden()
    assert calls == []

    page.get_by_test_id("plugs-pair").click()
    page.locator("#confirmOk").click()
    expect(page.locator("#toast")).to_have_text("No new devices")
    assert len(calls) == 1


def test_tuya_dimmer_brightness_slider_round_trips(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _boot_plugs(
        page, base_url, sample_units, _with_second_blind_and_light(sample_plugs),
        mock_api, mock_energy, mock_tuya,
    )

    # The dimmer's Brightness slider (#870) is in its light sheet (#884), at
    # the device's current level.
    row = page.locator('#lightsList [data-device-id="light-1"]')
    expect(row).to_have_class(re.compile(r"\blight-row\b"))
    row.locator(".action-row-main").click()
    expect(page.locator("#lightSheet")).to_be_visible()
    number = page.locator("#lightSheetControls .light-number")
    expect(number).to_have_value("40")
    assert_no_horizontal_overflow(page)

    # One request on commit, carrying the percentage; the row re-renders from
    # the read-back card.
    with page.expect_request("**/api/tuya/light-1/brightness") as request:
        number.fill("70")
        number.press("Enter")
    assert request.value.post_data_json == {"brightness": 70}
    expect(page.locator("#lightSheetControls .light-number")).to_have_value("70")
    expect(row.locator(".action-row-meta-text")).to_have_text("Off")
