"""Unit tests for :mod:`src.blind_automation` — the blind group move (#181).

The Tuya LAN is never touched: ``list_devices`` and ``set_cover`` are faked, so
these pin the fan-out contract — default target set, per-blind outcomes,
parallel dispatch, and the caller-bug errors — not TinyTuya.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src import blind_automation as B
from src.tuya_client import TuyaCommandError, TuyaDeviceInfo


def _info(device_id: str, *, cover: bool) -> TuyaDeviceInfo:
    return TuyaDeviceInfo(
        device_id=device_id,
        name=device_id,
        cover_control_dps="1" if cover else None,
        switch_dps=None if cover else "1",
    )


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Two blinds (one listed twice, as devices.json may) and a plug."""
    infos = [
        _info("blind-a", cover=True),
        _info("plug-x", cover=False),
        _info("blind-b", cover=True),
        _info("blind-a", cover=True),
    ]
    monkeypatch.setattr(B, "list_devices", lambda: infos)
    sent: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _set_cover(device_id: str, action: str) -> dict:
        with lock:
            sent.append((device_id, action))
        return {"dps": {"1": action}}

    monkeypatch.setattr(B, "set_cover", _set_cover)
    return sent


def test_cover_device_ids_lists_each_blind_once(fleet) -> None:
    assert B.cover_device_ids() == ["blind-a", "blind-b"]


def test_default_target_is_every_blind(fleet) -> None:
    outcomes = asyncio.run(B.move_blinds("open"))
    assert sorted(fleet) == [("blind-a", "open"), ("blind-b", "open")]
    assert all(outcome.ok for outcome in outcomes)


def test_explicit_targets_are_deduplicated(fleet) -> None:
    asyncio.run(B.move_blinds("close", ["blind-b", "blind-b"]))
    assert fleet == [("blind-b", "close")]


def test_one_failing_blind_does_not_stop_the_others(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _set_cover(device_id: str, action: str) -> dict:
        if device_id == "blind-a":
            raise TuyaCommandError("no response")
        fleet.append((device_id, action))
        return {}

    monkeypatch.setattr(B, "set_cover", _set_cover)
    outcomes = {o.device_id: o for o in asyncio.run(B.move_blinds("stop"))}
    assert outcomes["blind-a"].ok is False
    assert "no response" in (outcomes["blind-a"].error or "")
    assert outcomes["blind-b"].ok is True
    assert fleet == [("blind-b", "stop")]


def test_commands_go_out_in_parallel(fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both blinds must be mid-command at the same time, not one after another."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _slow_set_cover(device_id: str, action: str) -> dict:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.2)
        with lock:
            in_flight -= 1
        return {}

    monkeypatch.setattr(B, "set_cover", _slow_set_cover)
    asyncio.run(B.move_blinds("open"))
    assert peak == 2


@pytest.mark.parametrize(
    "action, ids, message",
    [
        ("raise", None, "action must be"),
        ("open", ["plug-x"], "not a blind: plug-x"),
        ("open", ["ghost"], "not a blind: ghost"),
    ],
)
def test_caller_bugs_raise_value_error(fleet, action, ids, message) -> None:
    with pytest.raises(ValueError, match=message):
        asyncio.run(B.move_blinds(action, ids))
    assert fleet == []
