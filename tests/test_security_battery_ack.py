"""Unit tests for the low-battery acknowledge logic (issue #221).

Covers the pure event-detection helpers in ``src.risco_client`` and the atomic
acknowledgment store in ``src.security_battery_ack``. No network, no app boot.
"""

from __future__ import annotations

from pathlib import Path

from src.risco_client import (
    SecurityEvent,
    is_battery_low_event,
    latest_battery_low_time,
)
from src.security_battery_ack import (
    clear_battery_ack,
    load_battery_ack,
    set_battery_ack,
)


def _ev(time: str, name: str) -> SecurityEvent:
    return SecurityEvent(time=time, name=name)


def test_is_battery_low_event_matches_low_not_restore() -> None:
    assert is_battery_low_event(_ev("t", "Device Battery Low - 'GARAJE'"))
    assert is_battery_low_event(_ev("t", "Low Battery - 'ROBERTO'"))
    # Restore / unrelated events must not match.
    assert not is_battery_low_event(_ev("t", "Device Battery Restore - 'GARAJE'"))
    assert not is_battery_low_event(_ev("t", "Zone Tamper - 'EXT COCINA'"))
    assert not is_battery_low_event(SecurityEvent(time="t"))


def test_latest_battery_low_time_picks_newest() -> None:
    events = [
        _ev("2026-06-27T11:00:00Z", "Device Battery Low - 'A'"),
        _ev("2026-06-25T09:00:00Z", "Device Battery Low - 'B'"),
        _ev("2026-06-28T08:00:00Z", "Device Battery Restore - 'A'"),  # ignored
        _ev("2026-06-26T10:00:00Z", "Zone Tamper - 'C'"),  # ignored
    ]
    assert latest_battery_low_time(events) == "2026-06-27T11:00:00Z"


def test_latest_battery_low_time_none_when_no_low_events() -> None:
    assert latest_battery_low_time([_ev("t", "Device Battery Restore - 'A'")]) is None
    assert latest_battery_low_time([]) is None


def test_battery_ack_store_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "security_battery_ack.json"
    assert load_battery_ack(p) is None  # never acknowledged

    set_battery_ack("2026-06-27T11:00:00Z", p)
    ack = load_battery_ack(p)
    assert ack == {"acknowledged": True, "low_event_time": "2026-06-27T11:00:00Z"}

    # Acknowledging with no known low event stores a null watermark.
    set_battery_ack(None, p)
    ack = load_battery_ack(p)
    assert ack == {"acknowledged": True, "low_event_time": None}

    clear_battery_ack(p)
    assert load_battery_ack(p) is None
    # Clearing an absent file is a no-op, not an error.
    clear_battery_ack(p)
