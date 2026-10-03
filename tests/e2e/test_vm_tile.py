"""The Home Assistant VM card on Home — status, power switch, failure states.

`#homeAssistantCard`'s VM half (#461): loading vs not-found, contextual
unavailability, keeping start usable for an identified-but-unreachable VM,
concise command-failure toasts, and stale-preserving poll failures. The same
card's voice-satellite half is covered by `test_home_assistant.py`.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, hold_reads
from tests.e2e._geometry import assert_no_overlap


def test_vm_tile_distinguishes_loading_from_not_found(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"hyperv": {"available": False, "state": "not_found"}}),
        ),
    )
    release = hold_reads(page, "/api/hyperv")
    boot_home(page, base_url)

    # #461/#805: status text in the read-only summary, switch in the body.
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "loading")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("Reading status…")
    expect(page.locator("#homeVmToggle")).to_be_disabled()
    release()
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "empty")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("VM not found")
    expect(page.locator("#homeVmToggle")).to_be_disabled()


def test_vm_tile_shows_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"Hyper-V host 192.0.2.80 timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("status unavailable")
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.80")


def test_vm_status_error_keeps_start_action_when_vm_is_identified(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": False,
                    "name": "Fixture HA",
                    "state": "unknown",
                    "error": "Get-VM status failed",
                }
            }),
        ),
    )
    page.route(
        "**/api/hyperv/start",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": True,
                    "name": "Fixture HA",
                    "state": "running",
                    "uptime_seconds": 0,
                }
            }),
        ),
    )
    boot_home(page, base_url)

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    # An unreachable-but-identified VM keeps the switch usable for start
    # (#461: the switch replaced the old tile's "Start Home Assistant").
    # #805: it is a body row, so the card is opened first (two taps).
    card = page.locator("#homeAssistantCard")
    card.locator("> summary").click()
    expect(card).to_have_attribute("open", "")
    toggle = page.locator("#homeVmToggle")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "true")
    # LAYOUT-05 (#805): the summary is read-only — no control inside it — and
    # switching the VM does not fold the card.
    expect(card.locator("> summary button, > summary input, > summary a")).to_have_count(0)
    expect(card).to_have_attribute("open", "")


def test_vm_command_failure_uses_concise_toast(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "hyperv": {
                    "available": False,
                    "name": "Fixture HA",
                    "state": "unknown",
                }
            }),
        ),
    )
    page.route(
        "**/api/hyperv/start",
        lambda route: route.fulfill(
            status=502,
            content_type="application/json",
            body='{"detail":"Start-VM host 192.0.2.80 Value cannot be null Parameter name: name"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#homeAssistantCard > summary").click()
    page.locator("#homeVmToggle").click()

    expect(page.locator("#toast")).to_have_text("Couldn't start Home Assistant")
    expect(page.locator("#toast")).not_to_contain_text("Start-VM")
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.80")


def test_vm_poll_failure_preserves_status_and_disables_power(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    failing = {"value": False}
    vm = {
        "available": True,
        "state": "running",
        "uptime_seconds": 3600,
        "ip_address": "192.0.2.81",
    }

    def handle_vm(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"Hyper-V host 192.0.2.80 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"hyperv": vm}),
        )

    page.route("**/api/hyperv", handle_vm)
    boot_home(page, base_url)
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(page.locator("#homeVmToggle")).to_be_enabled()

    failing["value"] = True
    page.locator("#tabAc").click()
    page.locator("#tabHome").click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "stale")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("online")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("cached")
    expect(page.locator("#homeVmToggle")).to_be_disabled()
    # The stale detail moved into the status text's tooltip (#461).
    expect(page.locator("#homeAssistantSummaryState")).to_have_attribute(
        "title", re.compile("Last updated .+ · live data unavailable")
    )
    expect(page.locator("#homeAssistantSummaryState")).not_to_contain_text("192.0.2.80")


def test_vm_switch_tap_zone_stays_out_of_the_card_summary(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """TOUCH-02 (#816): the body-row switch's tap zone must not reach the summary."""
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"hyperv": {
                "available": True, "name": "Fixture HA", "state": "running", "uptime_seconds": 60,
            }}),
        ),
    )
    boot_home(page, base_url)
    card = page.locator("#homeAssistantCard")
    card.locator("> summary").click()
    expect(page.locator("#homeVmToggle")).to_be_enabled()

    assert_no_overlap([card.locator("> summary"), page.locator("#homeVmToggle")])

