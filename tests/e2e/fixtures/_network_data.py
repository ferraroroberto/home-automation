"""Default LAN snapshot and internet history served by the ``mock_network`` fixture."""

from __future__ import annotations

from typing import Dict, List

_SEEN = 1_700_000_000


def _device(**fields) -> Dict:
    """One LAN device row: the common idle defaults, overridden per device."""
    row: Dict = {
        "mac": None, "ip": None, "name": None, "display_name": None,
        "vendor": None, "category": None, "conn_type": None,
        "is_wireless": True, "signal": None, "link_rate": None, "ssid": None,
        "source": "ap", "online": True, "important": False, "hidden": False,
        "is_new": False, "randomized": False, "group": None,
        "last_conn_type": None, "last_ssid": None,
        "first_seen": _SEEN, "last_seen": _SEEN, "times_seen": 1,
    }
    row.update(fields)
    return row


def _bssid(wifi_id: str, ssid: str, **fields) -> Dict:
    row: Dict = {
        "wifi_id": wifi_id, "ssid": ssid, "original_name": ssid, "bssid": wifi_id,
        "display_name": None, "hidden": False, "signal": None, "rssi_dbm": None,
        "channel": None, "band": None, "radio_type": None,
        "authentication": "WPA2-Personal", "encryption": "CCMP",
        "connected": False, "channel_width_mhz": None,
    }
    row.update(fields)
    return row


def _devices() -> List[Dict]:
    return [
        _device(
            mac="AA:00:00:00:00:01", ip="192.0.2.11", name="Zebra Phone",
            display_name="Zebra Phone", vendor="Apple", category="phone",
            conn_type="5GHz", signal=30, link_rate=300, ssid="TestNet-5",
            times_seen=3,
        ),
        _device(
            mac="AA:00:00:00:00:02", ip="192.0.2.12", name="Alpha Laptop",
            display_name="Alpha Laptop", vendor="Asus", category="computer",
            conn_type="5GHz", signal=72, link_rate=866, ssid="TestNet-5",
            times_seen=2,
        ),
        _device(
            mac="AA:00:00:00:00:03", ip="192.0.2.13", name="Kitchen Speaker",
            display_name="Kitchen Speaker", vendor="Amazon", category="iot",
            conn_type="2.4GHz", signal=55, link_rate=72, ssid="TestNet-IoT",
        ),
        _device(
            mac="AA:00:00:00:00:04", ip="192.0.2.14", name="NAS",
            display_name="NAS", vendor="Synology", category="nas",
            conn_type="wired", is_wireless=False, link_rate=1000,
        ),
    ]


def _wifi() -> Dict:
    return {
        "available": True,
        "interface_name": "Wi-Fi",
        "adapter_description": "Fixture WLAN",
        "current_ssid": "TestNet-5",
        "current_bssid": "AA:BB:CC:DD:EE:01",
        "current_signal": 86,
        "current_channel": 44,
        "current_band": "5GHz",
        "current_radio_type": "802.11ac",
        "recommendations": ["Current Wi-Fi signal is strong (86%)."],
        "error": None,
        "bssids": [
            _bssid(
                "AA:BB:CC:DD:EE:01", "TestNet-5", signal=86, rssi_dbm=-57,
                channel=44, band="5GHz", radio_type="802.11ac", connected=True,
            ),
            _bssid(
                "AA:BB:CC:DD:EE:02", "TestNet-IoT", signal=55, rssi_dbm=-73,
                channel=6, band="2.4GHz", radio_type="802.11n",
            ),
        ],
    }


def default_snapshot() -> Dict:
    return {
        "internet": {
            "online": True,
            "external_ms": 14,
            "gateway_ms": 0,
            "packet_loss_pct": 0,
            "download_mbps": None,
            "upload_mbps": None,
            "speedtest_server": None,
        },
        "access_point": {
            "reachable": True,
            "model": "R9000",
            "mode": "access_point",
            "firmware": "V1.0.5.42",
            "device_count": 4,
            "error": None,
        },
        "router": {
            "reachable": True,
            "authenticated": True,
            "model": "ZXHN F6600P",
            "wan_online": True,
            "public_ip": "203.0.113.24",
            "uptime_s": 19_380,
            "error": None,
        },
        "wifi": _wifi(),
        "devices": _devices(),
    }


def default_internet_history() -> Dict:
    """Deterministic series for the internet tile's sparklines (#840): a day of
    latency with a spike, and three speed tests. Fixture values, no real test."""
    return {
        "latency": [
            [_SEEN + i * 900, 12 + (i * 7) % 9 + (40 if i == 40 else 0)]
            for i in range(96)
        ],
        "download": [[1_699_000_000, 310.0], [1_699_500_000, 280.0], [_SEEN, 325.0]],
        "upload": [[1_699_000_000, 42.0], [1_699_500_000, 38.0], [_SEEN, 44.0]],
        "latency_window_h": 24,
        "speed_window_d": 30,
    }
