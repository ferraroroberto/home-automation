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
    assert delays[0] <= T.BackoffTracker().base_s
    assert all(delays[i] <= delays[i + 1] + 0.01 for i in range(len(delays) - 1))
    assert delays[-1] <= T.BackoffTracker().max_s + 0.01


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


@pytest.mark.parametrize(
    "row, is_light",
    [
        # A dimmer switch: lighting category + the lighting switch code.
        ({"category": "dj", "mapping": {"1": {"code": "switch_led"}, "2": {"code": "bright_value"}}}, True),
        # A light in a light category whose switch uses a generic code.
        ({"category": "tgq", "mapping": {"1": {"code": "switch_1"}}}, True),
        # An uncategorised device whose switch is the lighting code.
        ({"mapping": {"20": {"code": "switch_led"}}}, True),
        # A smart plug — even one feeding a lamp — stays a plug.
        ({"category": "cz", "mapping": {"1": {"code": "switch_1"}}}, False),
        # A blind has no switch at all.
        ({"category": "qt", "mapping": {"1": {"code": "control"}}}, False),
        # A light category with no switch mapping is nothing to toggle.
        ({"category": "dj", "mapping": {}}, False),
    ],
)
def test_sanitize_flags_lights(row: dict, is_light: bool) -> None:
    """Tuya lights are told apart from plugs so the PWA lists them under Lights (#181)."""
    info = T._sanitize({"id": "dev-1", "name": "Fixture", **row})
    assert info.is_light is is_light


_DIMMER = {
    "id": "dimmer-1", "name": "Fixture dimmer", "category": "dj", "ip": "192.0.2.9",
    "key": "k", "version": "3.3",
    "mapping": {
        "1": {"code": "switch_led", "type": "Boolean", "values": {}},
        "2": {"code": "bright_value", "type": "Integer",
              "values": {"min": 25, "max": 255, "scale": 0, "step": 1}},
    },
}


def _bright(row: dict = _DIMMER) -> T.TuyaMapping:
    mapping = T._first_mapping(row, T._BRIGHTNESS_CODES)
    assert mapping is not None
    return mapping


@pytest.mark.parametrize("pct, raw", [(1, 25), (100, 255), (50, 139)])
def test_brightness_maps_percent_onto_the_device_range(pct: int, raw: int) -> None:
    """1 % is the device's own min, 100 % its max, linear between (#870)."""
    assert T.brightness_to_raw(pct, _bright()) == raw
    assert T.raw_to_brightness(raw, _bright()) == pct


def test_brightness_falls_back_to_the_code_default_range() -> None:
    row = {"mapping": {"2": {"code": "bright_value_v2", "type": "Integer"}}}
    assert T.brightness_to_raw(1, _bright(row)) == 10
    assert T.brightness_to_raw(100, _bright(row)) == 1000
    assert T.raw_to_brightness("junk", _bright(row)) is None


def test_sanitize_reports_brightness_dps_only_for_dimmers() -> None:
    assert T._sanitize(_DIMMER).brightness_dps == "2"
    plug = {"id": "p", "category": "cz", "mapping": {"1": {"code": "switch_1"}}}
    assert T._sanitize(plug).brightness_dps is None


def test_read_device_state_reports_brightness_percent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_devices(path, [_DIMMER])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    T._backoff_state.clear()
    monkeypatch.setattr(T, "_status", lambda device_id: {"dps": {"1": True, "2": 255}})
    state = T.read_device_state("dimmer-1")
    assert state["switch_on"] is True
    assert state["brightness_pct"] == 100


class _FakeDevice:
    def __init__(self) -> None:
        self.writes: list[tuple[str, object]] = []

    def set_value(self, dps: str, value: object) -> dict:
        self.writes.append((dps, value))
        return {"dps": {dps: value}}


def test_set_brightness_writes_only_the_brightness_dps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_devices(path, [_DIMMER])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    fake = _FakeDevice()
    monkeypatch.setattr(T, "_connect", lambda device_id, cls=None: fake)
    T.set_brightness("dimmer-1", 100)
    assert fake.writes == [("2", 255)]


@pytest.mark.parametrize("pct", [0, 101, True, 50.5])
def test_set_brightness_rejects_out_of_range(pct: object) -> None:
    with pytest.raises(ValueError, match="1 to 100"):
        T.set_brightness("dimmer-1", pct)  # type: ignore[arg-type]


def test_set_brightness_rejects_a_device_without_brightness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "devices.json"
    _write_devices(path, [{"id": "p", "ip": "192.0.2.8", "key": "k",
                           "mapping": {"1": {"code": "switch_1"}}}])
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    with pytest.raises(ValueError, match="no brightness"):
        T.set_brightness("p", 50)


# ------------------------------------------------------------- set_cover (#899)
def _write_blind(path: Path, code: str = "control") -> None:
    _write_devices(
        path,
        [{
            "id": "blind-x", "name": "Test Blind", "key": "0123456789abcdef",
            "ip": "10.9.9.9", "version": "3.3",
            "mapping": {"1": {"code": code, "type": "Enum"}},
        }],
    )


def _fake_link(monkeypatch: pytest.MonkeyPatch, reply: object, status: object = None) -> list:
    """TinyTuya's socket layer faked: every frame is recorded and answered.

    A ``status()`` query is answered with ``status`` (DPS 1 still at
    ``reply``'s value when omitted); every other frame with ``reply``.
    """
    import tinytuya

    frames: list = []
    if status is None:
        status = reply

    def _generate_payload(self, command, data=None, *_a, **_kw):
        frames.append((command, data))
        return command

    def _send_receive(self, payload, *_a, **_kw):
        return status if payload == tinytuya.DP_QUERY else reply

    monkeypatch.setattr(tinytuya.Device, "generate_payload", _generate_payload)
    monkeypatch.setattr(tinytuya.Device, "_send_receive", _send_receive)
    return frames


@pytest.fixture(autouse=True)
def _clear_cover_types() -> None:
    """Isolate the per-device cover-type cache between tests."""
    T._cover_types.clear()
    yield
    T._cover_types.clear()


def test_set_cover_raises_when_the_blind_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Stop that never landed (#899): TinyTuya's error reply must surface.

    ``CoverDevice.stop_cover`` returned ``None``, so this logged as sent and
    nothing ever retried it.
    """
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    _fake_link(monkeypatch, tinytuya.error_json(tinytuya.ERR_CONNECT))

    with pytest.raises(T.TuyaCommandError, match="Err 901"):
        T.set_cover("blind-x", "stop")


@pytest.mark.parametrize("code, value", [("control", "stop"), ("mach_operate", "STOP")])
def test_set_cover_writes_the_mapped_dps_in_one_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: str, value: str
) -> None:
    """Once the blind's cover type is known, a command is one control frame."""
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path, code)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    frames = _fake_link(monkeypatch, {"dps": {"1": value}})

    T.set_cover("blind-x", "stop")
    frames.clear()
    T.set_cover("blind-x", "stop")
    assert frames == [(tinytuya.CONTROL, {"1": value})]


@pytest.mark.parametrize(
    "current, action, value",
    [
        # Type 3, string-numeric: the blinds that ignored 'close' after #899 (#901).
        ("1", "open", "1"), ("1", "close", "2"), ("1", "stop", "0"),
        ("2", "open", "1"), ("0", "close", "2"),
        # Type 1, Tuya's own enum.
        ("open", "close", "close"), ("stop", "open", "open"), ("close", "stop", "stop"),
        # Type 8, the vendor mach_operate set.
        ("FZ", "open", "ZZ"), ("ZZ", "close", "FZ"), ("STOP", "stop", "STOP"),
    ],
)
def test_set_cover_writes_the_value_set_the_blind_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current: str, action: str, value: str
) -> None:
    """The value written comes from the blind's local status, not the cloud schema (#901).

    A blind whose ``status()`` reports DPS 1 as ``'1'`` acks a write of
    ``'close'`` and ignores it; it has to be sent ``'2'``.
    """
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    frames = _fake_link(monkeypatch, {"dps": {"1": value}}, status={"dps": {"1": current}})

    T.set_cover("blind-x", action)
    assert frames[-1] == (tinytuya.CONTROL, {"1": value})


def test_set_cover_detects_the_cover_type_once_per_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cover type is cached: one ``status()`` read, then control frames only."""
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    frames = _fake_link(monkeypatch, {"dps": {"1": "2"}}, status={"dps": {"1": "1"}})

    T.set_cover("blind-x", "close")
    T.set_cover("blind-x", "open")
    T.set_cover("blind-x", "stop")

    assert [cmd for cmd, _ in frames].count(tinytuya.DP_QUERY) == 1
    assert [data for cmd, data in frames if cmd == tinytuya.CONTROL] == [
        {"1": "2"}, {"1": "1"}, {"1": "0"},
    ]


@pytest.mark.parametrize("status", [{"dps": {}}, {"dps": {"1": "sideways"}}, {"dps": {"1": None}}])
def test_set_cover_refuses_to_guess_an_unknown_cover_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """No known value set at the control DPS: a distinct, logged error, nothing written."""
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    frames = _fake_link(monkeypatch, {"dps": {"1": "close"}}, status=status)

    with caplog.at_level("ERROR", logger=T.logger.name), pytest.raises(T.TuyaCoverTypeUnknown):
        T.set_cover("blind-x", "close")

    assert tinytuya.CONTROL not in [cmd for cmd, _ in frames]
    assert "cover type" in caplog.text
    assert "blind-x" not in T._cover_types  # a later status may resolve it


def test_set_cover_unsupported_action_for_the_type_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relay-style cover (type 2) has no stop value: refuse rather than write ``None``."""
    import tinytuya

    path = tmp_path / "devices.json"
    _write_blind(path)
    monkeypatch.setattr(T, "_DEVICE_FILE", path)
    frames = _fake_link(monkeypatch, {"dps": {"1": True}}, status={"dps": {"1": True}})

    with pytest.raises(T.TuyaCommandError, match="no stop value"):
        T.set_cover("blind-x", "stop")
    assert tinytuya.CONTROL not in [cmd for cmd, _ in frames]
