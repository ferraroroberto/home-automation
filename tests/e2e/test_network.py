"""Network tab mobile layout and attached-device sorting."""

from __future__ import annotations

import re
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home


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
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()

    expect(page.locator("#netInternetStatus")).to_have_text("Online")
    # Attached devices is collapsed by default now; open it for the inventory.
    devices_card = page.locator("details.net-devices-card")
    expect(devices_card).not_to_have_attribute("open", "")
    page.locator("details.net-devices-card > summary").click()
    expect(devices_card).to_have_attribute("open", "")
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


def test_network_tab_shows_loading_before_first_result(
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
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/network' || url.endsWith('/api/network')) {
            return new Promise(function(resolve, reject) {
              setTimeout(function() {
                originalFetch(input, init).then(resolve, reject);
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    boot_home(page, base_url)
    page.locator("#tabNetwork").click()

    expect(page.locator("#paneNetwork")).to_have_attribute("data-state", "loading")
    expect(page.locator("#netFeedback .empty-state-message")).to_have_text(
        "Reading network status…"
    )
    expect(page.locator("#paneNetwork")).to_have_attribute("data-state", "ready")
    expect(page.locator("#netInternetStatus")).to_have_text("Online")


def test_network_tab_shows_contextual_unavailable_state(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/network**",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"router 192.0.2.1 timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabNetwork").click()

    expect(page.locator("#paneNetwork")).to_have_attribute("data-state", "error")
    expect(page.locator("#netFeedback .empty-state-message")).to_have_text(
        "Network unavailable"
    )
    expect(page.locator("#netInternetStatus")).to_be_hidden()
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.1")


def test_network_poll_failure_preserves_and_labels_last_good_data(
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
    boot_home(page, base_url)
    page.locator("#tabNetwork").click()
    expect(page.locator("#netInternetStatus")).to_have_text("Online")

    page.unroute("**/api/network**")
    page.route(
        "**/api/network**",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"router 192.0.2.1 timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabHome").click()
    page.locator("#tabNetwork").click()

    expect(page.locator("#paneNetwork")).to_have_attribute("data-state", "stale")
    expect(page.locator("#netFeedback")).to_contain_text("Last updated")
    expect(page.locator("#netFeedback")).to_contain_text("live data unavailable")
    expect(page.locator("#netInternetStatus")).to_have_text("Online")
    expect(page.locator("#netApReboot")).to_be_disabled()
    expect(page.locator("#netRouterReboot")).to_be_disabled()
    expect(page.locator("#netFeedback")).not_to_contain_text("192.0.2.1")


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
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()
    page.locator("details.net-devices-card > summary").click()  # collapsed by default now

    chips = page.locator("#netStats .net-stat-chip")
    expect(chips).to_have_count(4)
    widths = chips.evaluate_all(
        "(nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().width))"
    )
    assert len(set(widths)) == 1

    # Order-row and Group-row pills render at matching widths (#519): A-Z/Signal
    # line up with My groups/Band instead of each sizing to its own text.
    sort_pills = page.locator(
        "#netSortAlpha, #netSortSignal, #netGroupByGroup, #netGroupByBand"
    )
    pill_widths = sort_pills.evaluate_all(
        "(nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().width))"
    )
    assert len(set(pill_widths)) == 1

    offline = page.locator("#netOfflineToggle")
    # Label reflects current visibility, not the pending action (#519); default
    # grouping is now "My groups" (#519), so this exercises the toggle there.
    expect(offline).to_have_text("Offline hidden")
    head_box = page.locator(".net-devices-head").bounding_box()
    offline_box = offline.bounding_box()
    assert head_box is not None
    assert offline_box is not None
    assert abs(
        (offline_box["x"] + offline_box["width"]) -
        (head_box["x"] + head_box["width"])
    ) <= 1

    offline.click()
    expect(offline).to_have_text("Offline shown")


def _lease_only_device() -> Dict:
    """A device the router still leases but nothing can vouch for (#550).

    The shape the DHCP lease table produces once its holder has left: in the
    read (``online: True``) with no band, no SSID and no signal to show for it.
    """
    return {
        "mac": "AA:00:00:00:00:06",
        "ip": "192.0.2.16",
        "name": "Ghost Phone",
        "display_name": "Ghost Phone",
        "vendor": "Fixture",
        "category": "phone",
        "conn_type": None,
        "is_wireless": False,
        "signal": None,
        "link_rate": None,
        "ssid": None,
        "source": "router",
        "online": True,
        "important": False,
        "hidden": False,
        "is_new": False,
        "randomized": False,
        "group": None,
        "last_conn_type": None,
        "last_ssid": None,
        "first_seen": 1_700_000_000,
        "last_seen": 1_700_000_100,
        "times_seen": 9,
    }


def test_network_offline_toggle_hides_devices_with_no_live_link(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """With Offline hidden, only a live signal or a wired link earns a row (#550).

    A lease-only device is in the current read, so the pre-#550 list rendered it
    as online — with an empty signal cell — and counted it in the group header.
    It now rides the Offline toggle instead, while the wired fixture device
    (wired, no signal) stays visible throughout.
    """
    mock_api(sample_units)
    mock_energy()
    snapshot = mock_network()
    snapshot["devices"].append(_lease_only_device())
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()
    page.locator("details.net-devices-card > summary").click()

    rows = page.locator("#netDevices .net-device")
    ghost = rows.filter(has_text="Ghost Phone")
    offline = page.locator("#netOfflineToggle")

    # "My groups" is the default (#519): one Unclassified group holding all five,
    # of which four have a live link. The ghost counts toward the total but not
    # toward "online", and its row is not rendered.
    head = page.locator("#netDevices .net-group-head")
    expect(head).to_have_count(1)
    expect(head.first).to_contain_text("4/5 online")
    expect(rows).to_have_count(4)
    expect(ghost).to_have_count(0)
    # Wired with no signal reading is still a link — it must not be filtered out.
    expect(rows.filter(has_text="NAS")).to_have_count(1)
    expect(offline).to_be_visible()
    expect(offline).to_have_text("Offline hidden")

    # Revealing them dims the ghost and says why the row was held back.
    offline.click()
    expect(offline).to_have_text("Offline shown")
    expect(rows).to_have_count(5)
    expect(ghost).to_have_class(re.compile(".*is-nolink.*"))
    expect(ghost).to_contain_text("no link")

    # The detail modal calls it a missing link, never "Offline" — the read never
    # established the device is gone, only that nothing vouches for it.
    ghost.locator(".net-device-name").click()
    expect(page.locator("#netDeviceDialog")).to_be_visible()
    expect(page.locator("#netDeviceStatus")).to_contain_text("No live link")
    expect(page.locator("#netDeviceSignal")).to_have_text("No link")
    page.locator("#netDeviceDetailClose").click()

    # Same rule in the band view: hidden by default, then its own "No link"
    # group rather than being mixed into the genuinely-absent ones.
    offline.click()
    expect(offline).to_have_text("Offline hidden")
    page.locator("#netGroupByBand").click()
    expect(ghost).to_have_count(0)
    expect(page.locator("#netDevices .net-group-head").filter(has_text="No link")).to_have_count(0)

    offline.click()
    expect(page.locator("#netDevices .net-group-head").filter(has_text="No link")).to_have_text(
        "No link · 1"
    )
    expect(ghost).to_have_count(1)


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
    boot_home(page, base_url)

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

    page.locator("details.net-devices-card > summary").click()  # collapsed by default now
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


def test_network_device_groups_create_move_rename_and_delete(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """The "My groups" view: create, move, auto-drop empty, rename, delete (#513);
    the default view + offline toggle behaving the same as the band view (#519)."""
    mock_api(sample_units)
    mock_energy()
    snapshot = mock_network()
    # An offline device stays visible (shaded) in its group once the offline
    # toggle is on, with its last-known band and SSID still readable.
    snapshot["devices"].append({
        "mac": "AA:00:00:00:00:05",
        "ip": "192.0.2.15",
        "name": "Offline Tablet",
        "display_name": "Offline Tablet",
        "vendor": "Fixture",
        "category": "tablet",
        "conn_type": None,
        "is_wireless": False,
        "signal": None,
        "link_rate": None,
        "ssid": None,
        "source": "history",
        "online": False,
        "important": False,
        "hidden": False,
        "is_new": False,
        "randomized": False,
        "group": None,
        "last_conn_type": "2.4GHz",
        "last_ssid": "TestNet-IoT",
        "first_seen": 1_700_000_000,
        "last_seen": 1_700_000_100,
        "times_seen": 4,
    })
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()
    page.locator("details.net-devices-card > summary").click()

    # "My groups" is the default/first grouping now (#519) — no click needed.
    expect(page.locator("#netGroupByGroup")).to_have_class("net-sort-btn active")

    rows = page.locator("#netDevices .net-device")
    heads = page.locator("#netDevices .net-group-head")
    # Nothing assigned yet: one synthetic Unclassified group holding every
    # device. The offline device counts toward the group's online/total header
    # regardless, but its row is hidden by default — the offline toggle now
    # governs visibility in the grouped view too, same as the band view (#519).
    expect(heads).to_have_count(1)
    expect(heads.first).to_contain_text("Unclassified")
    expect(heads.first).to_contain_text("4/5 online")
    expect(rows).to_have_count(4)
    # Unclassified is synthetic — it can't be renamed or deleted.
    expect(page.locator("#netDevices .net-group-edit")).to_have_count(0)

    # Toggling offline on reveals the shaded row, with its last-known band and
    # SSID readable but no MAC (dropped from the grouped view — #519).
    offline_toggle = page.locator("#netOfflineToggle")
    expect(offline_toggle).to_have_text("Offline hidden")
    offline_toggle.click()
    expect(offline_toggle).to_have_text("Offline shown")
    expect(rows).to_have_count(5)
    offline_row = page.locator("#netDevices .net-device").filter(has_text="Offline Tablet")
    expect(offline_row).to_have_class(re.compile(".*is-offline.*"))
    expect(offline_row).to_contain_text("2.4 GHz")
    expect(offline_row).to_contain_text("TestNet-IoT")
    expect(offline_row).not_to_contain_text("AA:00:00:00:00:05")

    # Create a group from the detail modal, then put a second device in it.
    _assign_group(page, "Alpha Laptop", new_name="Elgato lights")
    _assign_group(page, "Kitchen Speaker", existing="Elgato lights")

    group_head = page.locator("#netDevices .net-group-head").filter(has_text="Elgato lights")
    expect(group_head).to_have_count(1)
    expect(group_head).to_contain_text("2/2 online")
    expect(page.locator("#netDevices .net-device")).to_have_count(5)

    # Move the last device out of a one-device group → the group disappears.
    _assign_group(page, "NAS", new_name="Temp")
    expect(page.locator("#netDevices .net-group-head").filter(has_text="Temp")).to_have_count(1)
    _assign_group(page, "NAS", existing="")
    expect(page.locator("#netDevices .net-group-head").filter(has_text="Temp")).to_have_count(0)

    # Rename the group; both members follow it.
    group_head.locator(".net-group-edit").click()
    expect(page.locator("#netGroupDialog")).to_be_visible()
    expect(page.locator("#netGroupMembers")).to_have_text("2 devices")
    page.locator("#netGroupName").fill("Luces")
    page.locator("#netGroupName").press("Enter")
    renamed = page.locator("#netDevices .net-group-head").filter(has_text="Luces")
    expect(renamed).to_have_count(1)
    expect(renamed).to_contain_text("2/2 online")

    # Delete it: the members fall back to Unclassified, nothing is lost.
    renamed.locator(".net-group-edit").click()
    page.locator("#netGroupDelete").click()
    page.locator("#confirmOk").click()
    expect(page.locator("#netDevices .net-group-head")).to_have_count(1)
    expect(page.locator("#netDevices .net-group-head").first).to_contain_text("Unclassified")
    expect(page.locator("#netDevices .net-device")).to_have_count(5)

    # The choice persists across a reload, and so do the assignments.
    _assign_group(page, "Alpha Laptop", new_name="Elgato lights")
    page.reload(wait_until="domcontentloaded")
    page.locator("#tabNetwork").click()
    page.locator("details.net-devices-card > summary").click()
    expect(page.locator("#netGroupByGroup")).to_have_class("net-sort-btn active")
    expect(
        page.locator("#netDevices .net-group-head").filter(has_text="Elgato lights")
    ).to_have_count(1)


def _assign_group(page: Page, device: str, existing: str = None, new_name: str = None) -> None:
    """Open one device's detail modal and stage + save a group assignment."""
    page.locator("#netDevices .net-device-name").filter(has_text=device).first.click()
    expect(page.locator("#netDeviceDialog")).to_be_visible()
    if new_name is not None:
        page.locator("#netDeviceGroup").select_option("__new__")
        page.locator("#netDeviceGroupNew").fill(new_name)
    else:
        page.locator("#netDeviceGroup").select_option(existing)
    page.locator("#netDeviceSave").click()
    page.locator("#netDeviceDetailClose").click()
    expect(page.locator("#netDeviceDialog")).to_be_hidden()


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
    boot_home(page, base_url)

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
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()

    expect(page.locator("#netFeedback .empty-state-message")).to_have_text(
        "Network unavailable"
    )
    expect(page.locator("#netInternetStatus")).to_have_text("Online", timeout=20_000)
    expect(page.locator("#netFeedback")).to_be_hidden()
    expect(page.locator("#netDevices .net-device-name-text").first).to_have_text("Alpha Laptop")


def test_network_walk_test_picks_a_device_and_records_a_room(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
    mock_network: Callable,
) -> None:
    """The walk test's whole flow: pick this device once, then record a room.

    The recorded signal must be the one the *access point* reports for the
    chosen client (30% for the fixture phone), not anything the browser could
    have measured — that inversion is the reason the feature exists (#547).
    """
    mock_api(sample_units)
    mock_energy()
    mock_network()
    boot_home(page, base_url)

    page.locator("#tabNetwork").click()
    page.locator("details.net-survey-card > summary").click()
    expect(page.locator("details.net-survey-card")).to_have_attribute("open", "")

    # Nothing recorded and no device chosen: the empty state names the next step
    # and Record stays disabled until there is a subject to measure.
    expect(page.locator("#netSurveyRooms .empty-state")).to_contain_text("pick this device")
    record = page.get_by_test_id("net-survey-record")
    expect(record).to_be_disabled()

    page.get_by_test_id("net-survey-pick").click()
    picker = page.locator("#netSurveyDialogList .net-survey-picker-row")
    expect(picker.first).to_be_visible()
    picker.filter(has_text="Zebra Phone").click()
    expect(page.locator("#netSurveyDeviceName")).to_have_text("Zebra Phone")
    expect(record).to_be_enabled()

    page.get_by_test_id("net-survey-room").fill("Kitchen")
    record.click()

    row = page.locator("#netSurveyRooms .net-survey-row")
    expect(row).to_have_count(1, timeout=30_000)
    expect(row).to_contain_text("Kitchen")
    expect(row).to_contain_text("30%")
    # Which radio heard it is the column an AP-placement decision turns on.
    expect(row).to_contain_text("via AP")
    expect(row).to_have_class(re.compile("is-weak"))
