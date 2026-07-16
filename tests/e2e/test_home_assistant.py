"""Issue #239: folded Home Assistant card + streamed room push-to-talk."""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


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


def test_card_is_folded_in_existing_position_and_loads_ha_only_when_open(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    calls = {"ha": 0}

    def handle_ha(route) -> None:
        calls["ha"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_HA_BODY))

    page.route("**/api/ha", handle_ha)
    _boot(page, base_url, sample_units, mock_api, mock_energy)

    card = page.locator("#homeAssistantCard")
    expect(card).not_to_have_attribute("open", "")
    expect(page.locator("#haSatellitesList")).to_be_hidden()
    assert calls["ha"] == 0
    card_summary_icon = card.locator("> summary .collapse-icon")
    expect(card_summary_icon.locator("use")).to_have_attribute("href", "#i-house")
    ha_icon = card_summary_icon.bounding_box()
    wake_icon = page.locator(".wake-alarms-card summary .collapse-icon").bounding_box()
    assert ha_icon is not None and wake_icon is not None
    assert ha_icon["width"] == wake_icon["width"]
    assert ha_icon["height"] == wake_icon["height"]
    ha_icon_offset = card_summary_icon.evaluate(
        "icon => icon.getBoundingClientRect().top - icon.closest('summary').getBoundingClientRect().top"
    )
    wake_icon_offset = page.locator(".wake-alarms-card summary .collapse-icon").evaluate(
        "icon => icon.getBoundingClientRect().top - icon.closest('summary').getBoundingClientRect().top"
    )
    assert abs(ha_icon_offset - wake_icon_offset) < 0.1
    assert page.locator("#homeEnergyFlow").evaluate(
        "(energy, card) => energy.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING",
        card.element_handle(),
    )

    card.locator("> summary").click()
    expect(card).to_have_attribute("open", "")
    # #461: the VM surface is the summary itself — status text + power switch.
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(page.locator("#homeVmToggle")).to_have_attribute("aria-checked", "true")

    # #461: everything but the uptime tile lives in Presence-style nested
    # subsections, all folded by default, each with a hit-target summary.
    for section_id in ("haSatellitesCard", "haInteractionsCard", "haHelpCard", "voiceCommandsCard"):
        section = page.locator("#" + section_id)
        expect(section).not_to_have_attribute("open", "")
        box = section.locator("summary").bounding_box()
        assert box is not None and box["height"] >= 44, section_id

    page.locator("#haSatellitesCard summary").click()
    expect(page.locator('.ha-satellite-row[data-entity="assist_satellite.kitchen"]')).to_contain_text(
        "Kitchen"
    )
    expect(page.locator('.ha-satellite-row[data-entity="assist_satellite.kitchen"]')).to_contain_text(
        "Volume 75%"
    )
    expect(page.locator('.ha-satellite-row[data-entity="assist_satellite.bedroom"] .ha-mic-btn')).to_be_disabled()

    page.locator("#haInteractionsCard summary").click()
    expect(page.locator(".ha-interaction-row")).to_contain_text("Where is mom?")

    help_card = page.locator("#haHelpCard")
    help_card.locator("summary").click()
    expect(help_card).to_have_attribute("open", "")
    expect(help_card).to_contain_text("See the voice layer")
    expect(help_card).to_contain_text("Talk to a room")
    expect(help_card).to_contain_text("Review recent interactions")
    expect(help_card).to_contain_text("Room and satellite names are owned in Home Assistant")
    assert calls["ha"] >= 1


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
    page.locator("#homeAssistantCard > summary").click()
    page.locator("#haSatellitesCard summary").click()

    row = page.locator('.ha-satellite-row[data-entity="assist_satellite.kitchen"]')
    mic = row.locator(".ha-mic-btn")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))
    expect(row.locator(".ha-live-transcript")).to_have_text("live partial")

    mic.click()
    expect(row.locator(".ha-live-transcript")).to_contain_text("final message · Announced")
    assert chunks == ["POST"]
    assert announced == [{"text": "final message"}]
