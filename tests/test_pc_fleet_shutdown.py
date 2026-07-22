"""Unit tests for the UPS-triggered PC-fleet shutdown orchestration (#498).

Covers :func:`app.webapp.power_notify._shutdown_fleet_satellites` (the hub
sweep: ordering, exclusion, already-down, confirmation, hub-unreachable) and
its integration into :func:`app.webapp.power_notify.record_low_battery_shutdown`
(satellites before the tower, degraded Telegram text, tower shuts down even
when a satellite fails). All hub HTTP is served by an in-process
``httpx.MockTransport`` — zero real network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.webapp import power_notify
from app.webapp.power_notify import (
    _shutdown_fleet_satellites,
    record_low_battery_shutdown,
)
from src import activity_log
from src.pc_fleet_prefs import PcFleetPrefs


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


async def _async_noop(*_args, **_kwargs) -> None:
    return None


def _machine(mid: str, *, state: str = "up", is_host: bool = False, name: Optional[str] = None) -> Dict[str, Any]:
    return {"id": mid, "display_name": name or mid, "state": state, "is_host": is_host}


def _make_handler(
    machines: List[Dict[str, Any]],
    *,
    shutdown_status: Optional[Dict[str, tuple]] = None,
    second_poll: Optional[List[Dict[str, Any]]] = None,
    order: Optional[List[str]] = None,
):
    """Build an httpx.MockTransport handler for the hub status + shutdown API."""
    shutdown_status = shutdown_status or {}
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/machines/status"):
            polls["n"] += 1
            data = second_poll if (polls["n"] >= 2 and second_poll is not None) else machines
            return httpx.Response(200, json={"active_id": "tower", "machines": data})
        if path.endswith("/shutdown"):
            mid = path.split("/machines/", 1)[1].rsplit("/", 1)[0]
            if order is not None:
                order.append(mid)
            code, detail = shutdown_status.get(mid, (200, None))
            if code == 200:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(code, json={"detail": detail or "err"})
        return httpx.Response(404, json={"detail": "not found"})

    return handler


def _patch_hub(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(**kwargs):
        return real(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 5.0))

    monkeypatch.setattr(power_notify.httpx, "AsyncClient", factory)
    # Confirmation sleep must not actually block the (fast) test suite.
    monkeypatch.setattr(power_notify.asyncio, "sleep", _async_noop)


def _read_power_log(tmp_path: Path) -> List[dict]:
    path = tmp_path / "power.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------- _shutdown_fleet_satellites


def test_fleet_skips_host_excluded_and_already_down(monkeypatch) -> None:
    machines = [
        _machine("tower", state="self", is_host=True),
        _machine("mac-mini-m4", state="up"),
        _machine("openclaw", state="down"),      # already off — normal, non-error
        _machine("gaming", state="up"),
    ]
    _patch_hub(monkeypatch, _make_handler(machines))
    result = asyncio.run(_shutdown_fleet_satellites(excluded=("gaming",), confirm_wait_s=0))

    assert result.hub_reachable is True
    outcomes = {o.machine_id: o.outcome for o in result.outcomes}
    assert "tower" not in outcomes                        # the host is never in the sweep
    assert outcomes["mac-mini-m4"] == "shutdown sent"
    assert outcomes["openclaw"] == "already down, skipped"
    assert outcomes["gaming"] == "excluded"


def test_fleet_failed_post_recorded(monkeypatch) -> None:
    machines = [_machine("mac-mini-m4"), _machine("openclaw")]
    handler = _make_handler(machines, shutdown_status={"openclaw": (502, "ssh refused")})
    _patch_hub(monkeypatch, handler)
    result = asyncio.run(_shutdown_fleet_satellites(excluded=(), confirm_wait_s=0))

    outcomes = {o.machine_id: o.outcome for o in result.outcomes}
    assert outcomes["mac-mini-m4"] == "shutdown sent"
    assert outcomes["openclaw"].startswith("failed:")
    assert "ssh refused" in outcomes["openclaw"]


def test_fleet_confirmation_annotates_down(monkeypatch) -> None:
    machines = [_machine("mac-mini-m4", state="up")]
    second = [_machine("mac-mini-m4", state="down")]
    _patch_hub(monkeypatch, _make_handler(machines, second_poll=second))
    result = asyncio.run(_shutdown_fleet_satellites(excluded=(), confirm_wait_s=1))

    outcomes = {o.machine_id: o.outcome for o in result.outcomes}
    assert outcomes["mac-mini-m4"] == "shutdown sent, confirmed down"


def test_fleet_hub_status_error_is_unreachable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    _patch_hub(monkeypatch, handler)
    result = asyncio.run(_shutdown_fleet_satellites(excluded=(), confirm_wait_s=0))

    assert result.hub_reachable is False
    assert result.outcomes == []


def test_fleet_hub_connect_error_is_unreachable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_hub(monkeypatch, handler)
    result = asyncio.run(_shutdown_fleet_satellites(excluded=(), confirm_wait_s=0))

    assert result.hub_reachable is False
    assert result.outcomes == []


# ----------------------------- record_low_battery_shutdown integration


def test_satellites_shut_down_before_tower(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    order: List[str] = []
    machines = [
        _machine("tower", state="self", is_host=True),
        _machine("mac-mini-m4"),
        _machine("openclaw"),
    ]
    _patch_hub(monkeypatch, _make_handler(machines, order=order))
    notifier = FakeNotifier()

    def tower_shutdown(**_kw) -> bool:
        order.append("tower")
        return True

    result = asyncio.run(record_low_battery_shutdown(
        detail="8min runtime",
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: notifier,
        shutdown_fn=tower_shutdown,
    ))

    assert result is True
    assert order[-1] == "tower"                           # tower goes last
    assert set(order[:-1]) == {"mac-mini-m4", "openclaw"}  # satellites first
    # Telegram lists per-machine outcomes + the tower-last note.
    msg = notifier.sent[0]
    assert "mac-mini-m4: shutdown sent" in msg
    assert "openclaw: shutdown sent" in msg
    assert "Tower shutting down last" in msg
    # Activity log carries the per-machine result list.
    log = _read_power_log(tmp_path)[0]
    assert log["event"] == "low_battery_shutdown"
    assert log["hub_reachable"] is True
    assert {m["id"] for m in log["fleet"]} == {"mac-mini-m4", "openclaw"}


def test_failed_satellite_still_shuts_down_tower(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    machines = [_machine("mac-mini-m4"), _machine("openclaw")]
    handler = _make_handler(machines, shutdown_status={"openclaw": (502, "ssh refused")})
    _patch_hub(monkeypatch, handler)
    notifier = FakeNotifier()
    tower_calls: List[dict] = []

    result = asyncio.run(record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: notifier,
        shutdown_fn=lambda **kw: tower_calls.append(kw) or True,
    ))

    assert result is True                       # a failed satellite never blocks the tower
    assert len(tower_calls) == 1
    assert "openclaw: failed:" in notifier.sent[0]


def test_hub_unreachable_degrades_to_tower_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_hub(monkeypatch, handler)
    notifier = FakeNotifier()
    tower_calls: List[dict] = []

    result = asyncio.run(record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=True),
        notifier_factory=lambda: notifier,
        shutdown_fn=lambda **kw: tower_calls.append(kw) or True,
    ))

    assert result is True                       # tower still shuts down
    assert len(tower_calls) == 1
    msg = notifier.sent[0]
    assert "Hub unreachable" in msg
    assert "Tower shutting down last" in msg
    log = _read_power_log(tmp_path)[0]
    assert log["hub_reachable"] is False
    assert log["fleet"] == []


def test_master_off_touches_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(activity_log, "LOGS_DIR", tmp_path)
    # If the hub were touched the mock isn't installed → any HTTP would error;
    # assert instead that neither the notifier nor the tower fire.
    notifier = FakeNotifier()
    tower_calls: List[dict] = []

    result = asyncio.run(record_low_battery_shutdown(
        pc_fleet_loader=lambda: PcFleetPrefs(enabled=False),
        notifier_factory=lambda: notifier,
        shutdown_fn=lambda **kw: tower_calls.append(kw) or True,
    ))

    assert result is False
    assert notifier.sent == []
    assert tower_calls == []
    log = _read_power_log(tmp_path)[0]
    assert log["event"] == "low_battery_shutdown"
    assert log["fleet_enabled"] is False
    assert "fleet" not in log
