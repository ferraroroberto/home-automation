"""Unit tests for :mod:`src.tuya_client` LAN-rescan address reconciliation.

Covers the Refresh-path logic that recovers stale ``devices.json`` IPs after a
plug takes a new DHCP lease (issue #166): merge-by-device-id, no-IP recovery,
key/mapping preservation, idempotency, and the atomic write. The UDP broadcast
scan itself is faked — these tests never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import tuya_client as T


def _write_devices(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_devices(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw["devices"] if isinstance(raw, dict) else raw


def test_apply_discovered_updates_stale_ip_and_keeps_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A moved plug's IP is reconciled while its key and DPS mapping survive."""
    path = tmp_path / "devices.json"
    _write_devices(
        path,
        {
            "timestamp": 1,
            "devices": [
                {
                    "id": "plug-a",
                    "name": "Estufa",
                    "ip": "192.168.0.35",          # stale
                    "key": "secret-key-a",
                    "version": "3.3",
                    "mapping": {"1": {"code": "switch_1"}},
                }
            ],
        },
    )
    monkeypatch.setattr(T, "_DEVICE_FILE", path)

    updated = T._apply_discovered_addresses({"plug-a": {"ip": "192.168.0.65", "version": 3.3}})

    assert updated == ["plug-a"]
    dev = _read_devices(path)[0]
    assert dev["ip"] == "192.168.0.65"
    assert dev["key"] == "secret-key-a"           # secret untouched
    assert dev["mapping"] == {"1": {"code": "switch_1"}}  # mapping untouched
    # The snapshot wrapper is preserved, not flattened to a bare list.
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    assert not (tmp_path / "devices.json.tmp").exists()


def test_apply_discovered_recovers_device_with_no_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A device stored with an empty IP gains the freshly-scanned address."""
    path = tmp_path / "devices.json"
    _write_devices(path, [{"id": "plug-b", "name": "Cortinas", "ip": "", "key": "k"}])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)

    updated = T._apply_discovered_addresses({"plug-b": {"ip": "192.168.0.61", "version": 3.3}})

    assert updated == ["plug-b"]
    assert _read_devices(path)[0]["ip"] == "192.168.0.61"


def test_apply_discovered_is_idempotent_when_ip_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unchanged address neither reports an update nor rewrites the file."""
    path = tmp_path / "devices.json"
    _write_devices(path, [{"id": "plug-c", "ip": "192.168.0.61", "key": "k"}])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    before = path.read_text(encoding="utf-8")

    updated = T._apply_discovered_addresses({"plug-c": {"ip": "192.168.0.61", "version": 3.3}})

    assert updated == []
    assert path.read_text(encoding="utf-8") == before  # no rewrite


def test_apply_discovered_aligns_legacy_address_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rows using the legacy ``address`` key get both fields reconciled."""
    path = tmp_path / "devices.json"
    _write_devices(path, [{"id": "plug-d", "address": "192.168.0.9", "key": "k"}])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)

    T._apply_discovered_addresses({"plug-d": {"ip": "192.168.0.99", "version": 3.3}})

    dev = _read_devices(path)[0]
    assert dev["ip"] == "192.168.0.99"
    assert dev["address"] == "192.168.0.99"


def test_rescan_addresses_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``rescan_addresses`` reports responders, updated ids, and the IP map."""
    path = tmp_path / "devices.json"
    _write_devices(
        path,
        [
            {"id": "plug-a", "ip": "192.168.0.35", "key": "k"},  # stale → updated
            {"id": "plug-c", "ip": "192.168.0.61", "key": "k"},  # already current
        ],
    )
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    monkeypatch.setattr(
        T,
        "_scan_lan",
        lambda _scan_time: {
            "plug-a": {"ip": "192.168.0.65", "version": 3.3},
            "plug-c": {"ip": "192.168.0.61", "version": 3.3},
        },
    )

    summary = T.rescan_addresses()

    assert summary["found"] == 2
    assert summary["updated"] == ["plug-a"]            # only the moved one
    assert summary["addresses"]["plug-a"] == "192.168.0.65"


def test_rescan_addresses_no_responders_is_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty scan never rewrites the file and reports nothing recovered."""
    path = tmp_path / "devices.json"
    _write_devices(path, [{"id": "plug-a", "ip": "192.168.0.35", "key": "k"}])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    monkeypatch.setattr(T, "_scan_lan", lambda _scan_time: {})
    before = path.read_text(encoding="utf-8")

    summary = T.rescan_addresses()

    assert summary == {"found": 0, "updated": [], "addresses": {}}
    assert path.read_text(encoding="utf-8") == before


def test_scan_lan_filters_invalid_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_scan_lan`` keeps only valid IPv4 responders from the raw scanner output."""
    monkeypatch.setattr(
        T.tuya_scanner,
        "devices",
        lambda **_kw: {
            "good": {"ip": "192.168.0.5", "version": 3.3},
            "bad-ip": {"ip": "Auto", "version": 3.3},
            "no-ip": {"version": 3.3},
        },
    )

    result = T._scan_lan(0.1)

    assert set(result) == {"good"}
    assert result["good"]["ip"] == "192.168.0.5"


# --------------------------------------------------------------- per-device backoff (issue #537)
@pytest.fixture(autouse=True)
def _clear_backoff_state() -> None:
    """Isolate the module-level backoff dict between tests."""
    T._backoff_state.clear()
    yield
    T._backoff_state.clear()


def test_seconds_until_retry_is_none_for_a_clean_device() -> None:
    assert T._seconds_until_retry("dev-1") is None


def test_record_failure_escalates_and_caps_at_max() -> None:
    delays = []
    for _ in range(8):
        T._record_backoff_failure("dev-1")
        delays.append(T._seconds_until_retry("dev-1"))

    # Strictly increasing until it hits the cap.
    assert delays[0] <= T._BACKOFF_BASE_S
    assert all(delays[i] <= delays[i + 1] + 0.01 for i in range(len(delays) - 1))
    assert delays[-1] <= T._BACKOFF_MAX_S + 0.01


def test_record_success_clears_backoff() -> None:
    T._record_backoff_failure("dev-1")
    assert T._seconds_until_retry("dev-1") is not None

    T._record_backoff_success("dev-1")

    assert T._seconds_until_retry("dev-1") is None


def _write_mapped_device(path: Path, device_id: str = "dev-1") -> None:
    _write_devices(
        path,
        [
            {
                "id": device_id,
                "name": "Test plug",
                "ip": "192.168.0.50",
                "key": "secret",
                "version": "3.3",
                "mapping": {"1": {"code": "switch_1"}},
            }
        ],
    )


def test_read_device_state_skips_network_while_backed_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A device within its backoff window never reaches ``_status`` at all."""
    path = tmp_path / "devices.json"
    _write_mapped_device(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    T._record_backoff_failure("dev-1")

    def _status_should_not_be_called(*_a, **_kw):
        raise AssertionError("_status must not be called while backed off")

    monkeypatch.setattr(T, "_status", _status_should_not_be_called)

    with pytest.raises(T.TuyaBackoffActive):
        T.read_device_state("dev-1")


def test_read_device_state_records_failure_and_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_mapped_device(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)

    def _fail(*_a, **_kw):
        raise T.TuyaCommandError("Device Unreachable (Err 905)")

    monkeypatch.setattr(T, "_status", _fail)

    with pytest.raises(T.TuyaCommandError):
        T.read_device_state("dev-1")

    assert T._seconds_until_retry("dev-1") is not None


def test_read_device_state_success_clears_prior_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_mapped_device(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    T._record_backoff_failure("dev-1")
    T._backoff_state["dev-1"].next_retry_at = 0.0  # let this attempt through

    monkeypatch.setattr(T, "_status", lambda *_a, **_kw: {"dps": {"1": True}})

    result = T.read_device_state("dev-1")

    assert result["reachable"] is True
    assert T._seconds_until_retry("dev-1") is None


def test_set_switch_bypasses_backoff_and_updates_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manual command always goes out immediately, even while backed off,
    and its outcome still updates the shared backoff bookkeeping."""
    path = tmp_path / "devices.json"
    _write_mapped_device(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    T._record_backoff_failure("dev-1")  # device is currently backed off

    class _FakeDevice:
        def set_value(self, dps, on):
            return {"dps": {dps: on}}

    monkeypatch.setattr(T, "_connect", lambda device_id, cls=None: _FakeDevice())

    result = T.set_switch("dev-1", True)

    assert result == {"dps": {"1": True}}
    assert T._seconds_until_retry("dev-1") is None  # success cleared the backoff


def test_set_switch_failure_also_escalates_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_mapped_device(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)

    class _FakeDevice:
        def set_value(self, dps, on):
            return {"Err": 905, "Error": "Device Unreachable"}

    monkeypatch.setattr(T, "_connect", lambda device_id, cls=None: _FakeDevice())

    with pytest.raises(T.TuyaCommandError):
        T.set_switch("dev-1", True)

    assert T._seconds_until_retry("dev-1") is not None
