"""Unit tests for :mod:`src.presence_client`.

Pure normalization and auth-control tests: no Apple network calls, no real
credentials, and no committed home coordinates.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from src import presence_client as P
from src.location_config import LocationConfig


class _FakeDevices:
    def __init__(self, devices: list[object]) -> None:
        self._devices = devices
        self.refreshed = False

    def refresh(self, locate: bool = True) -> None:
        self.refreshed = locate

    def __iter__(self):
        return iter(self._devices)


class _FakeApi:
    def __init__(self, *, requires_2fa: bool = False, validates: bool = True) -> None:
        self.requires_2fa = requires_2fa
        self.validates = validates
        self.trusted = False
        self.devices = _FakeDevices([])

    def validate_2fa_code(self, code: str) -> bool:
        self.requires_2fa = False
        return self.validates and code == "123456"

    def trust_session(self) -> None:
        self.trusted = True


def test_distance_m_is_reasonable_for_nearby_points() -> None:
    # Roughly 111 m per 0.001 degree latitude at the equator.
    assert 110 <= P.distance_m(0, 0, 0.001, 0) <= 112


def test_entity_from_device_normalizes_location_and_home_distance() -> None:
    device = type("Device", (), {})()
    device.data = {
        "id": "dev-1",
        "name": "Test Phone",
        "deviceDisplayName": "iPhone",
        "deviceClass": "iPhone",
        "batteryLevel": 0.57,
        "batteryStatus": "Charging",
        "location": {
            "latitude": 0.0,
            "longitude": 0.0,
            "horizontalAccuracy": 12.4,
            "timeStamp": 1_700_000_000_000,
        },
    }

    entity = P._entity_from_device(device, LocationConfig(lat=0.001, lon=0.0))

    assert entity.entity_id == "dev-1"
    assert entity.name == "Test Phone"
    assert entity.model == "iPhone"
    assert entity.device_class == "iPhone"
    assert entity.latitude == 0.0
    assert entity.longitude == 0.0
    assert entity.horizontal_accuracy_m == 12.4
    assert entity.battery_level_pct == 57
    assert entity.battery_status == "Charging"
    assert entity.last_seen is not None
    assert entity.last_seen.tzinfo == timezone.utc
    assert 100 <= entity.distance_from_home_m <= 120
    assert entity.at_home is True


def test_entity_from_device_marks_location_outside_home_radius_as_away() -> None:
    device = type("Device", (), {})()
    device.data = {
        "id": "dev-2",
        "name": "Away Phone",
        "location": {"latitude": 0.01, "longitude": 0.0},
    }

    entity = P._entity_from_device(
        device,
        LocationConfig(lat=0.0, lon=0.0),
        home_radius_m=200,
    )

    assert entity.distance_from_home_m is not None
    assert entity.distance_from_home_m > 1000
    assert entity.at_home is False


def test_entity_from_device_tolerates_missing_location() -> None:
    device = type("Device", (), {})()
    device.data = {"id": "tag-1", "name": "Keys", "batteryLevel": None}

    entity = P._entity_from_device(device)

    assert entity.name == "Keys"
    assert entity.has_location is False
    assert entity.latitude is None
    assert entity.battery_level_pct is None
    assert entity.at_home is None


def test_load_presence_config_reads_home_radius(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(P, "load_dotenv", lambda override=True: None)
    monkeypatch.setenv("ICLOUD_EMAIL", "fixture@example.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "secret")
    monkeypatch.setenv("PRESENCE_HOME_RADIUS_M", "150")

    cfg = P.load_presence_config(session_dir=tmp_path)

    assert cfg.home_radius_m == 150
    assert cfg.session_dir == tmp_path


def test_2fa_without_code_raises_actionable_error() -> None:
    api = _FakeApi(requires_2fa=True)

    with pytest.raises(P.PresenceAuthError, match="requires 2FA"):
        P._complete_2fa(api, verification_code=None, trust_session=True)


def test_2fa_with_code_validates_and_trusts_session() -> None:
    api = _FakeApi(requires_2fa=True)

    P._complete_2fa(api, verification_code="123456", trust_session=True)

    assert api.trusted is True
    assert api.requires_2fa is False


def test_iter_devices_refreshes_with_location() -> None:
    devices = _FakeDevices([object()])

    out = list(P._iter_devices(devices))

    assert len(out) == 1
    assert devices.refreshed is True
