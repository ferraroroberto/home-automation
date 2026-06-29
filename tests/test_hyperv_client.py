"""Unit tests for the pure logic in :mod:`src.hyperv_client` (issue #240).

No Hyper-V, no subprocess: only the JSON status parsing, the error
classification, the state/MAC normalisation, and the config-name guard — the
parts that must be correct before a real VM is ever touched.
"""

from __future__ import annotations

import pytest

from src import hyperv_client as H


# ------------------------------------------------------------ status parsing
def test_parse_running_vm_with_ip_and_mac() -> None:
    raw = (
        '{"Name":"Home Assistant","State":"Running","UptimeSeconds":274800,'
        '"Ip":"192.168.0.4","Mac":"00155D012A0B"}'
    )
    s = H.parse_vm_status(raw)
    assert s.available is True
    assert s.name == "Home Assistant"
    assert s.state == "running"
    assert s.uptime_seconds == 274800
    assert s.ip_address == "192.168.0.4"
    # MAC is colon-formatted upper-case for display.
    assert s.mac_address == "00:15:5D:01:2A:0B"


def test_parse_off_vm_has_null_ip() -> None:
    raw = '{"Name":"Home Assistant","State":"Off","UptimeSeconds":0,"Ip":null,"Mac":"00155D012A0B"}'
    s = H.parse_vm_status(raw)
    assert s.state == "off"
    assert s.uptime_seconds == 0
    assert s.ip_address is None
    assert s.mac_address == "00:15:5D:01:2A:0B"


def test_parse_falls_back_to_name_when_payload_lacks_it() -> None:
    s = H.parse_vm_status('{"State":"Off","UptimeSeconds":0,"Ip":null,"Mac":null}', name="HA")
    assert s.name == "HA"
    assert s.mac_address is None  # null MAC → None, never a placeholder string


def test_parse_drops_all_zero_placeholder_mac() -> None:
    # A dynamic-MAC adapter reads all-zero until the VM has booted once.
    s = H.parse_vm_status('{"Name":"HA","State":"Off","UptimeSeconds":0,"Ip":null,"Mac":"000000000000"}')
    assert s.mac_address is None


def test_to_dict_is_json_serialisable_shape() -> None:
    s = H.parse_vm_status('{"Name":"HA","State":"Running","UptimeSeconds":10,"Ip":"10.0.0.4","Mac":null}')
    d = s.to_dict()
    assert set(d) == {
        "available", "name", "state", "uptime_seconds",
        "ip_address", "mac_address", "error", "updated_at",
    }
    assert d["state"] == "running"


# ---------------------------------------------------------- error classifier
def test_classify_not_found() -> None:
    msg = "Hyper-V was unable to find a virtual machine with name \"Home Assistant\"."
    assert H.classify_powershell_error(msg) == "not_found"


def test_classify_permission() -> None:
    assert H.classify_powershell_error("Start-VM : The operation failed. Access denied.") == "permission"
    assert H.classify_powershell_error("You do not have the required permission.") == "permission"


def test_classify_unknown_falls_through() -> None:
    assert H.classify_powershell_error("The term 'Get-VM' is not recognized.") == "unknown"
    assert H.classify_powershell_error("") == "unknown"


# ------------------------------------------------------------ state normalise
def test_normalize_state_lowercases_enum() -> None:
    assert H._normalize_state("Running") == "running"
    assert H._normalize_state("Off") == "off"
    assert H._normalize_state(None) == "unknown"


# ------------------------------------------------------------- config guard
def test_vm_name_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_VM_NAME", raising=False)
    with pytest.raises(H.HyperVConfigError):
        H.vm_name()


def test_vm_name_returns_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_VM_NAME", "  Home Assistant  ")
    assert H.vm_name() == "Home Assistant"
