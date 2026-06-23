"""Network tab mobile layout and attached-device sorting."""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect


def _boot(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


def test_network_tab_groups_devices_and_switches_sort(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network()
    _boot(page, base_url)

    page.locator("#tabNetwork").click()

    expect(page.locator("#netInternetStatus")).to_have_text("Online")
    expect(page.locator("#netAlerts")).to_be_hidden()
    expect(page.locator("details.net-devices-card")).to_have_attribute("open", "")
    expect(page.locator("#netStats")).to_contain_text("1 Wired")
    expect(page.locator("#netStats")).to_contain_text("2 5 GHz")
    expect(page.locator("#netStats")).to_contain_text("1 2.4 GHz")
    expect(page.locator("#netStats")).to_contain_text("1 Weak")

    ap_meta = page.locator("#netApMeta .net-health-meta-line")
    expect(ap_meta).to_have_count(2)
    expect(ap_meta.nth(1)).to_have_text("FW V1.0.5.42 · 4 devices")
    router_meta = page.locator("#netRouterMeta .net-health-meta-line")
    expect(router_meta).to_have_count(2)
    expect(router_meta.nth(0)).to_have_text("WAN up · 203.0.113.24")
    expect(router_meta.nth(1)).to_have_text("up 5h 23m")

    names = page.locator("#netDevices .net-device-name-text")
    expect(names.nth(0)).to_have_text("Alpha Laptop")
    expect(page.locator("#netDevices .net-device-meta").nth(0)).to_contain_text("Wi-Fi TestNet-5")
    expect(page.locator("#netSortAlpha")).to_have_class("net-sort-btn active")

    page.locator("#netSortSignal").click()
    expect(names.nth(0)).to_have_text("Zebra Phone")
    expect(page.locator("#netSortSignal")).to_have_class("net-sort-btn active")

    page.locator("details.net-devices-card > summary").click()
    expect(page.locator("#netDevices")).to_be_hidden()
