"""Network tab visibility/display-name helper tests."""

from __future__ import annotations

from src.network_hidden import (
    load_hidden_device_macs,
    load_hidden_wifi_ids,
    normalize_wifi_id,
    set_device_hidden,
    set_wifi_hidden,
)
from src.network_wifi_display_names import (
    load_network_wifi_display_names,
    set_network_wifi_display_name,
)


def test_network_hidden_helpers_normalize_keys(tmp_path) -> None:
    device_store = tmp_path / "network_hidden.json"
    wifi_store = tmp_path / "network_wifi_hidden.json"

    set_device_hidden("aa:bb:cc:dd:ee:ff", True, path=device_store)
    assert load_hidden_device_macs(device_store) == {"AA:BB:CC:DD:EE:FF"}

    set_device_hidden("AA:BB:CC:DD:EE:FF", False, path=device_store)
    assert load_hidden_device_macs(device_store) == set()

    set_wifi_hidden("aa:bb:cc:dd:ee:01", True, path=wifi_store)
    set_wifi_hidden("SSID:Neighbour", True, path=wifi_store)
    assert load_hidden_wifi_ids(wifi_store) == {"AA:BB:CC:DD:EE:01", "SSID:Neighbour"}


def test_network_wifi_display_names_use_bssid_then_ssid_fallback(tmp_path) -> None:
    store = tmp_path / "network_wifi_display_names.json"

    assert normalize_wifi_id("aa:bb:cc:dd:ee:01", "Home") == "AA:BB:CC:DD:EE:01"
    assert normalize_wifi_id("", "Neighbour") == "SSID:Neighbour"

    set_network_wifi_display_name("aa:bb:cc:dd:ee:01", "Main AP", path=store)
    set_network_wifi_display_name("SSID:Neighbour", "Ignored neighbour", path=store)

    assert load_network_wifi_display_names(store) == {
        "AA:BB:CC:DD:EE:01": "Main AP",
        "SSID:Neighbour": "Ignored neighbour",
    }
