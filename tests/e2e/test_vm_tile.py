"""The Home Assistant VM row on Devices — status, power switch, failure states.

`#homeAssistantCard`'s VM half (#461, moved from Home to the Devices tab in
#884): loading vs not-found, contextual unavailability, keeping start usable
for an identified-but-unreachable VM, concise command-failure toasts, and
stale-preserving poll failures. The same group's voice-satellite half is
covered by `test_home_assistant.py`.
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
    page.locator("#tabIot").click()

    # #461/#884: status text in the VM row's meta line, switch as its trailing item.
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
    page.locator("#tabIot").click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    expect(page.locator("#homeAssistantSummaryState")).to_have_text("Status unavailable")
    expect(page.locator("#haVmAvatar")).to_have_attribute("data-badge", "down")
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
    page.locator("#tabIot").click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "error")
    # An unreachable-but-identified VM keeps the switch usable for start
    # (#461: the switch replaced the old tile's "Start Home Assistant").
    # #884: the group is always open - the switch is one tap away.
    card = page.locator("#homeAssistantCard")
    toggle = page.locator("#homeVmToggle")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("Online")
    expect(page.locator("#haVmAvatar")).to_have_attribute("data-badge", "up")
    expect(toggle).to_be_enabled()
    expect(toggle).to_have_attribute("aria-checked", "true")
    # LAYOUT-05 (#805, #884): the row's text block is read-only - the switch is
    # its sibling, not nested inside it - and switching the VM leaves the group visible.
    expect(card.locator(".ha-vm-row .action-row-main button, .ha-vm-row .action-row-main input")).to_have_count(0)
    expect(card.locator(".ha-vm-row > #homeVmToggle")).to_have_count(1)
    expect(card).to_be_visible()


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
    page.locator("#tabIot").click()
    expect(page.locator("#homeVmToggle")).to_be_enabled()
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
    page.locator("#tabIot").click()
    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "ready")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("Online")
    expect(page.locator("#homeVmToggle")).to_be_enabled()

    failing["value"] = True
    page.locator("#tabAc").click()
    page.locator("#tabIot").click()

    expect(page.locator("#homeAssistantCard")).to_have_attribute("data-vm-state", "stale")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("Online")
    expect(page.locator("#homeAssistantSummaryState")).to_contain_text("cached")
    expect(page.locator("#homeVmToggle")).to_be_disabled()
    # The stale detail moved into the status text's tooltip (#461).
    expect(page.locator("#homeAssistantSummaryState")).to_have_attribute(
        "title", re.compile("Last updated .+ · live data unavailable")
    )
    expect(page.locator("#homeAssistantSummaryState")).not_to_contain_text("192.0.2.80")


def test_vm_switch_tap_zone_stays_inside_its_row(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """TOUCH-02 (#816, #884): the VM switch's tap zone stays in its own row.

    The old assertion kept the switch out of the card summary; on the shared
    row the neighbours are the group's tap targets, so the switch's effective
    rectangle must not reach the satellites/interactions rows below it.
    """
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
    page.locator("#tabIot").click()
    toggle = page.locator("#homeVmToggle")
    expect(toggle).to_be_enabled()

    row_box = page.locator(".ha-vm-row").bounding_box()
    toggle_box = toggle.bounding_box()
    assert row_box is not None and toggle_box is not None
    assert toggle_box["x"] >= row_box["x"]
    assert toggle_box["x"] + toggle_box["width"] <= row_box["x"] + row_box["width"] + 0.5
    assert toggle_box["y"] >= row_box["y"] - 0.5
    assert toggle_box["y"] + toggle_box["height"] <= row_box["y"] + row_box["height"] + 0.5

    assert_no_overlap([toggle, page.locator("#haSatellitesOpen")])
