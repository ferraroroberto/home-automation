"""Shared parts 2 (#880, Step 2/8 of #872).

The page header's live line names only the exceptions (in their tone) and
otherwise a plain fact; the Security tab carries a count badge, attention for
detector trouble and danger while the alarm is triggered (decision 10); and
every detail sheet runs on one shell with two save models: a staged editor
discards on ×, Esc or the backdrop and hands focus back to its opener, an
instant sheet closes on Done. All devices are the suite's fakes.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings
from tests.e2e.test_contrast import _CONTRAST_JS

_ZONE = {"type": 1, "status": "closed", "active": False, "bypass": False,
         "triggered": False, "display_name": None, "hidden": False}


def _security(mode: str, troubled: int) -> Dict:
    zones = [{**_ZONE, "id": i, "name": f"Zone {i}", "trouble": i <= troubled}
             for i in range(1, 4)]
    return {"reachable": True, "label": mode.title(), "mode": mode,
            "supported_actions": ["partial", "perimeter", "arm"],
            "ac_lost": False, "assumed_control_panel_state": False, "zones": zones}


@pytest.mark.chromium_only
def test_header_lines_name_exceptions_else_a_plain_fact(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    mock_security: Callable, mock_presence: Callable,
) -> None:
    sample_units[1]["reachable"] = False  # one unit offline
    mock_api(sample_units)
    mock_energy()  # exporting 1,200 W
    mock_tuya(sample_plugs)  # one switch on, 1,450 W metered
    mock_presence()
    mock_security(_security("disarmed", troubled=1))
    page.route("**/api/ups", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"ups": null}'))
    boot_home(page, base_url)

    ac = page.locator('#paneAc .status[data-head="ac"]')
    expect(ac).to_have_text("1 offline")
    expect(ac.locator(".head-exception")).to_have_attribute("data-tone", "attention")

    page.locator("#tabEnergy").click()
    energy = page.locator('#paneEnergy .status[data-head="energy"]')
    expect(energy).to_have_text("Exporting 1,200 W")
    expect(energy.locator(".head-exception")).to_have_count(0)

    page.locator("#tabIot").click()
    expect(page.locator('#paneIot .status[data-head="iot"]')).to_have_text("1 on · 1,450 W")

    page.locator("#tabSecurity").click()
    security = page.locator('#paneSecurity .status[data-head="security"]')
    expect(security).to_have_text("1 trouble")
    # The exception is its tone's text colour, not the slot's muted grey.
    part = security.locator('.head-exception[data-tone="attention"]')
    assert part.evaluate("el => getComputedStyle(el).color") != security.evaluate(
        "el => getComputedStyle(el).color")


def test_security_badge_counts_trouble_and_turns_danger_when_triggered(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
    mock_security: Callable, mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_presence()
    mock_security(_security("disarmed", troubled=2))
    boot_home(page, base_url)

    tab = page.locator("#tabSecurity")
    badge = tab.locator(".tab-badge")
    expect(badge).to_have_text("2")
    expect(tab).to_have_accessible_name(re.compile(r"Security, 2 trouble"))
    expect(tab).not_to_have_attribute("data-badge-tone", "danger")
    attention_bg = badge.evaluate("el => getComputedStyle(el).backgroundColor")

    # Triggered outranks trouble: one danger badge, still AA on its fill.
    mock_security(_security("triggered", troubled=2))
    page.reload(wait_until="domcontentloaded")
    expect(badge).to_have_text("1")
    expect(tab).to_have_attribute("data-badge-tone", "danger")
    expect(tab).to_have_accessible_name(re.compile(r"Security, 1 triggered"))
    assert badge.evaluate("el => getComputedStyle(el).backgroundColor") != attention_bg
    assert badge.evaluate(_CONTRAST_JS) >= 4.5

    # Nothing wrong: no badge at all.
    mock_security(_security("armed", troubled=0))
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#homeSecurityActions .security-action")
    expect(badge).to_have_count(0)


@pytest.mark.chromium_only
def test_sheets_follow_their_save_model(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya(sample_plugs)
    mock_network()
    boot_home(page, base_url)

    # Staged: the plug editor. Esc and the backdrop discard the draft, and
    # focus goes back to the row that opened it.
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )
    opener = page.locator('[data-device-id="plug-1"] .device-row-name')
    dialog = page.locator("#plugDialog")
    expect(dialog).to_have_attribute("data-save-model", "staged")
    opener.click()
    expect(dialog).to_be_visible()
    page.locator("#plugDisplayName").fill("Draft name")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    opener.click()
    expect(page.locator("#plugDisplayName")).to_have_value("")
    page.mouse.click(4, 4)  # the backdrop
    expect(dialog).to_be_hidden()
    expect(opener).to_have_text("Test Heater")

    # Instant: the Wi-Fi sheet saves as it changes; its one primary is Done.
    open_settings(page)
    page.locator("details.net-wifi-card > summary").click()
    page.locator("#netWifiList .net-wifi-row").filter(has_text="TestNet-IoT") \
        .locator(".net-wifi-row-name").click()
    wifi = page.locator("#netWifiDialog")
    expect(wifi).to_have_attribute("data-save-model", "instant")
    page.locator("#netWifiDisplayName").fill("Neighbour AP")
    page.locator("#netWifiDone").click()
    expect(wifi).to_be_hidden()
    expect(page.locator("#netWifiList .net-wifi-row").filter(has_text="Neighbour AP")).to_have_count(1)
