"""Issues #239 / #884: the Home Assistant group on Devices + streamed room push-to-talk."""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import open_settings_sheet


_HA_BODY = {
    "satellites": [
        {
            "entity_id": "assist_satellite.kitchen",
            "name": "Kitchen Voice",
            "room": "Kitchen",
            "online": True,
            "state": "idle",
            "volume": 0.75,
            "media_player": "media_player.kitchen",
        },
        {
            "entity_id": "assist_satellite.bedroom",
            "name": "Bedroom Voice",
            "room": "Bedroom",
            "online": False,
            "state": "unavailable",
            "volume": 0.5,
            "media_player": "media_player.bedroom",
        },
    ],
    "interactions": [
        {
            "timestamp": "2026-07-15T19:08:45+00:00",
            "room": "Kitchen",
            "transcript": "Where is mom?",
            "intent_kind": "local",
            "intent": "Locate",
            "action": "Locate",
            "spoken_response": "Mom is home.",
        }
    ],
    "voice_transcriber": True,
}

_MEDIA_RECORDER = """
(() => {
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async () => ({
    getTracks: () => [{ stop: () => {} }],
  });
  class FakeRecorder {
    constructor(_stream, opts) {
      this.mimeType = (opts && opts.mimeType) || 'audio/webm';
      this.state = 'inactive';
      this.listeners = {};
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    start(_timeslice) { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      if (this.listeners.dataavailable) {
        this.listeners.dataavailable({data: new Blob(['fixture-audio'], {type: this.mimeType})});
      }
      if (this.listeners.stop) this.listeners.stop();
    }
  }
  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
"""


def _stub_vm(page: Page) -> None:
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hyperv": {
                        "available": True,
                        "name": "Home Assistant",
                        "state": "running",
                        "uptime_seconds": 7200,
                        "ip_address": "192.0.2.8",
                        "mac_address": "00:11:22:33:44:55",
                    }
                }
            ),
        ),
    )


def _boot(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    _stub_vm(page)
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


def test_group_is_on_devices_and_polls_ha_only_while_a_sheet_is_open(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    """#884: the HA group lives on Devices; /api/ha is read once on entry, then polled with a sheet open."""
    reads: List[str] = []

    def handle_ha(route) -> None:
        reads.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_HA_BODY))

    page.route("**/api/ha", handle_ha)
    page.clock.install()
    _boot(page, base_url, sample_units, mock_api, mock_energy)

    # Moved off Home: the group is a plain section on the Devices pane, nothing to open.
    expect(page.locator("#paneHome #homeAssistantCard")).to_have_count(0)
    expect(page.locator("#paneIot #homeAssistantCard")).to_have_count(1)
    expect(page.locator("#homeAssistantCard")).to_have_js_property("tagName", "SECTION")
    assert reads == []

    page.locator("#tabIot").click()
    card = page.locator("#homeAssistantCard")
    expect(card).to_be_visible()
    # Status in words on the VM row, power switch as its trailing item.
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("Online")
    expect(page.locator("#haVmAvatar")).to_have_attribute("data-badge", "up")
    expect(page.locator("#homeVmToggle")).to_have_attribute("aria-checked", "true")
    # One read on entry fills the rows' meta lines; both sheets are still closed.
    expect(page.locator("#haSatellitesMeta")).to_have_text("1 of 2 online")
    expect(page.locator("#haInteractionsMeta")).to_have_text(re.compile(r"^1 · last \d{1,2}:\d\d"))
    expect(page.locator("#haSatellitesSheet")).to_be_hidden()
    expect(page.locator("#haInteractionsSheet")).to_be_hidden()
    assert len(reads) == 1

    for opener in ("#haSatellitesOpen", "#haInteractionsOpen"):
        box = page.locator(opener).bounding_box()
        assert box is not None and box["height"] >= 44, opener

    # Sheets closed: time passing reads nothing.
    page.clock.run_for(45_000)
    page.evaluate("() => fetch('/healthz').then((r) => r.status)")
    assert len(reads) == 1

    # Voice satellites sheet: rows, volume, an offline room's disabled mic; polls while open.
    page.locator("#haSatellitesOpen").click()
    expect(page.locator("#haSatellitesSheet")).to_be_visible()
    kitchen = page.locator('.ha-satellite-row[data-entity="assist_satellite.kitchen"]')
    expect(kitchen).to_contain_text("Kitchen")
    expect(kitchen).to_contain_text("Volume 75%")
    expect(
        page.locator('.ha-satellite-row[data-entity="assist_satellite.bedroom"] .ha-mic-btn')
    ).to_be_disabled()
    with page.expect_request(lambda r: r.method == "GET" and r.url.endswith("/api/ha")):
        page.clock.run_for(15_000)
    page.locator("#haSatellitesSheetDone").click()
    expect(page.locator("#haSatellitesSheet")).to_be_hidden()

    # Recent interactions sheet.
    page.locator("#haInteractionsOpen").click()
    expect(page.locator("#haInteractionsSheet")).to_be_visible()
    expect(page.locator(".ha-interaction-row")).to_contain_text("Where is mom?")
    page.locator("#haInteractionsSheetDone").click()
    expect(page.locator("#haInteractionsSheet")).to_be_hidden()

    # Both closed again: the poll stops.
    page.evaluate("() => fetch('/healthz').then((r) => r.status)")
    settled = len(reads)
    page.clock.run_for(45_000)
    page.evaluate("() => fetch('/healthz').then((r) => r.status)")
    assert len(reads) == settled


def test_help_merged_into_the_voice_sheet(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    """#886: "What can I do?" (a Settings card since #884) is merged into
    Settings › Voice › What can I say?, and keeps its guidance."""
    _boot(page, base_url, sample_units, mock_api, mock_energy)
    expect(page.locator("#haHelpCard")).to_have_count(0)

    open_settings_sheet(page, "voiceSheet")
    help_section = page.locator("#voiceSheet #voiceAppHelp")
    expect(help_section).to_be_visible()
    expect(help_section).to_contain_text("See the voice layer")
    expect(help_section).to_contain_text("Talk to a room")
    expect(help_section).to_contain_text("Review recent interactions")
    expect(help_section).to_contain_text("Room and satellite names are owned in Home Assistant")


def test_streamed_partial_finishes_and_announces_to_selected_room(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    page.add_init_script(_MEDIA_RECORDER)
    announced = []
    chunks = []
    page.route(
        "**/api/ha",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HA_BODY)
        ),
    )
    page.route(
        "**/api/ha/transcribe/sessions",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"session_id":"vt-1"}'
        ),
    )
    page.route(
        "**/api/ha/transcribe/sessions/vt-1/events*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/event-stream",
            body='event: partial\ndata: {"version":1,"transcript":"live partial"}\n\n',
        ),
    )

    def handle_chunk(route) -> None:
        # WebKit's Playwright bridge does not expose Blob request bytes through
        # post_data_buffer, but reaching this handler proves the chunk POST.
        chunks.append(route.request.method)
        route.fulfill(status=200, content_type="application/json", body='{"raw_bytes":13}')

    page.route("**/api/ha/transcribe/sessions/vt-1/chunk", handle_chunk)
    page.route(
        "**/api/ha/transcribe/sessions/vt-1/finish",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"transcript":"final message","language":"en"}',
        ),
    )

    def handle_announce(route) -> None:
        announced.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"room":"Kitchen","text":"final message"}',
        )

    page.route("**/api/ha/satellites/assist_satellite.kitchen/announce", handle_announce)
    _boot(page, base_url, sample_units, mock_api, mock_energy)
    page.locator("#tabIot").click()
    page.locator("#haSatellitesOpen").click()
    expect(page.locator("#haSatellitesSheet")).to_be_visible()

    row = page.locator('.ha-satellite-row[data-entity="assist_satellite.kitchen"]')
    mic = row.locator(".ha-mic-btn")
    # #805: push-to-talk is the row's main action — a visible word, not just an icon.
    expect(mic).to_have_text("Talk")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))
    expect(mic).to_have_text("Stop")
    expect(row.locator(".ha-live-transcript")).to_have_text("live partial")

    mic.click()
    expect(row.locator(".ha-live-transcript")).to_contain_text("final message · Announced")
    assert chunks == ["POST"]
    assert announced == [{"text": "final message"}]


def test_recent_interactions_is_hidden_until_there_is_one(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    """#805: an empty Recent interactions box offers nothing, so it stays hidden."""
    body = {**_HA_BODY, "interactions": []}
    page.route(
        "**/api/ha",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(body)),
    )
    _boot(page, base_url, sample_units, mock_api, mock_energy)

    page.locator("#tabIot").click()
    # The satellites row's meta fills once the HA read has landed - the empty
    # interactions row is still not offered.
    expect(page.locator("#haSatellitesMeta")).to_have_text("1 of 2 online")
    expect(page.locator("#haInteractionsCard")).to_be_hidden()
    expect(page.locator("#haSatellitesOpen")).to_be_visible()

