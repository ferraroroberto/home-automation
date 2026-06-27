"""Network tab mobile layout and attached-device sorting."""

from __future__ import annotations

import re
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

    expect(page.locator("#netWifiStatus")).to_have_text("86%")
    expect(page.locator("#netWifiSummary")).to_contain_text("TestNet-5")
    page.locator("details.net-wifi-card > summary").click()
    expect(page.locator("#netWifiMeta")).to_contain_text("Fixture WLAN")
    expect(page.locator("#netWifiRecommendations")).to_contain_text("strong")
    expect(page.locator("#netWifiList .net-wifi-row")).to_have_count(2)
    current_wifi = page.locator("#netWifiList .net-wifi-row").filter(has_text="TestNet-5")
    expect(current_wifi).to_contain_text("current")
    wifi_canvas_sizes = page.locator(".net-wifi-chart canvas").evaluate_all(
        "(nodes) => nodes.map((node) => ({ width: node.width, height: node.height }))"
    )
    assert all(size["width"] > 0 and size["height"] > 0 for size in wifi_canvas_sizes)

    names = page.locator("#netDevices .net-device-name-text")
    expect(names.nth(0)).to_have_text("Alpha Laptop")
    expect(page.locator("#netDevices .net-device-meta").nth(0)).to_contain_text("Wi-Fi TestNet-5")
    expect(page.locator("#netSortAlpha")).to_have_class("net-sort-btn active")

    page.locator("#netSortSignal").click()
    expect(names.nth(0)).to_have_text("Zebra Phone")
    expect(page.locator("#netSortSignal")).to_have_class("net-sort-btn active")
    expect(page.locator("#netSortAlpha")).to_have_class("net-sort-btn")

    page.locator("details.net-devices-card > summary").click()
    expect(page.locator("#netDevices")).to_be_hidden()


def test_network_header_uses_equal_chips_and_compact_offline_toggle(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    snapshot = mock_network()
    snapshot["devices"].append({
        "mac": "AA:00:00:00:00:05",
        "ip": "192.0.2.15",
        "name": "Offline Tablet",
        "display_name": "Offline Tablet",
        "vendor": "Fixture",
        "category": "tablet",
        "conn_type": "5GHz",
        "is_wireless": True,
        "signal": None,
        "link_rate": None,
        "ssid": "TestNet-5",
        "source": "history",
        "online": False,
        "important": True,
        "is_new": False,
        "randomized": False,
        "first_seen": 1_700_000_000,
        "last_seen": 1_700_000_100,
        "times_seen": 4,
    })
    _boot(page, base_url)

    page.locator("#tabNetwork").click()

    chips = page.locator("#netStats .net-stat-chip")
    expect(chips).to_have_count(4)
    widths = chips.evaluate_all(
        "(nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().width))"
    )
    assert len(set(widths)) == 1

    offline = page.locator("#netOfflineToggle")
    expect(offline).to_have_text("Show offline")
    head_box = page.locator(".net-devices-head").bounding_box()
    offline_box = offline.bounding_box()
    assert head_box is not None
    assert offline_box is not None
    assert abs(
        (offline_box["x"] + offline_box["width"]) -
        (head_box["x"] + head_box["width"])
    ) <= 1

    offline.click()
    expect(offline).to_have_text("Hide offline")


def test_network_rename_and_hide_wifi_and_attached_device(
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
    page.locator("details.net-wifi-card > summary").click()

    wifi_row = page.locator("#netWifiList .net-wifi-row").filter(has_text="TestNet-IoT")
    wifi_row.locator(".net-wifi-row-name").click()
    expect(page.locator("#netWifiDialog")).to_be_visible()
    expect(page.locator("#netWifiOriginalName")).to_contain_text("Original SSID: TestNet-IoT")
    page.locator("#netWifiDisplayName").fill("Neighbour AP")
    page.locator("#netWifiDisplayName").press("Enter")
    expect(page.locator("#netWifiList .net-wifi-row").filter(has_text="Neighbour AP")).to_have_count(1)

    page.locator("#netWifiHiddenDetailToggle").click()
    page.locator("#netWifiDetailClose").click()
    expect(page.locator("#netWifiHiddenCount")).to_have_text("1 hidden")
    expect(page.locator("#netWifiHiddenToggle")).to_have_text("Show hidden")
    expect(page.locator("#netWifiList .net-wifi-row").filter(has_text="Neighbour AP")).to_have_count(0)

    page.locator("#netWifiHiddenToggle").click()
    hidden_wifi = page.locator("#netWifiList .net-wifi-row").filter(has_text="Neighbour AP")
    expect(hidden_wifi).to_have_count(1)
    expect(hidden_wifi).to_have_class(re.compile(".*is-hidden.*"))

    device_button = page.locator("#netDevices .net-device-name").filter(has_text="Alpha Laptop")
    device_button.click()
    expect(page.locator("#netDeviceDialog")).to_be_visible()
    page.locator("#netDeviceDisplayName").fill("Office Laptop")
    page.locator("#netDeviceDisplayName").press("Enter")
    expect(page.locator("#netDevices .net-device-name-text").filter(has_text="Office Laptop")).to_have_count(1)

    # Hidden now stages and commits on Save (#203); close alone would discard.
    page.locator("#netDeviceHiddenToggle").click()
    page.locator("#netDeviceSave").click()
    page.locator("#netDeviceDetailClose").click()
    expect(page.locator("#netHiddenCount")).to_have_text("1 hidden")
    expect(page.locator("#netHiddenToggle")).to_have_text("Show hidden")
    expect(page.locator("#netDevices .net-device-name-text").filter(has_text="Office Laptop")).to_have_count(0)

    page.locator("#netHiddenToggle").click()
    hidden_device = page.locator("#netDevices .net-device").filter(has_text="Office Laptop")
    expect(hidden_device).to_have_count(1)
    expect(hidden_device).to_have_class(re.compile(".*is-hidden.*"))


def test_network_wifi_header_stays_quiet_when_scan_unavailable(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    snapshot = mock_network()
    snapshot["wifi"] = {
        "available": False,
        "interface_name": "Wi-Fi",
        "adapter_description": "Fixture WLAN",
        "current_ssid": None,
        "current_bssid": None,
        "current_signal": None,
        "current_channel": None,
        "current_band": None,
        "current_radio_type": None,
        "recommendations": [],
        "error": "Wi-Fi diagnostics are unavailable in this fixture.",
        "bssids": [],
    }
    _boot(page, base_url)

    page.locator("#tabNetwork").click()

    expect(page.locator("#netWifiStatus")).to_have_text("")
    expect(page.locator("#netWifiSummary")).to_have_text("")
    header_text = page.locator("details.net-wifi-card > summary").inner_text()
    assert "Scan" not in header_text
    assert "Scan available" not in header_text
    assert "Unavailable" not in header_text

    page.locator("details.net-wifi-card > summary").click()
    expect(page.locator("#netWifiNote")).to_contain_text(
        "Wi-Fi diagnostics are unavailable in this fixture."
    )


def test_network_tab_retries_after_first_load_failure(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_network(failures_before_success=1)
    _boot(page, base_url)

    page.locator("#tabNetwork").click()

    expect(page.locator("#netDevicesNote")).to_contain_text("Temporary network read failure")
    expect(page.locator("#netInternetStatus")).to_have_text("Online", timeout=20_000)
    expect(page.locator("#netDevicesNote")).to_be_hidden()
    expect(page.locator("#netDevices .net-device-name-text").first).to_have_text("Alpha Laptop")
