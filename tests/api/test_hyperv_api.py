"""API smoke for the Hyper-V VM control endpoints.

``GET/POST /api/hyperv[/start|/stop]`` serialise + drive the VM state —
Hyper-V faked throughout, no host I/O.
"""

from __future__ import annotations

from typing import List

import pytest
from fastapi.testclient import TestClient


def test_hyperv_route_runs_with_monkeypatched_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/hyperv`` serialises the VM state — Hyper-V faked, no host I/O."""
    from src.hyperv_client import HyperVState

    state = HyperVState(
        available=True,
        name="Home Assistant",
        state="running",
        uptime_seconds=274800,
        ip_address="192.168.0.4",
        mac_address="00:15:5D:01:2A:0B",
    )
    monkeypatch.setattr("app.webapp.routers.hyperv.fetch_hyperv_state", lambda: state)

    resp = client.get("/api/hyperv")
    assert resp.status_code == 200
    body = resp.json()["hyperv"]
    assert body["state"] == "running"
    assert body["ip_address"] == "192.168.0.4"
    assert body["mac_address"] == "00:15:5D:01:2A:0B"
    assert body["uptime_seconds"] == 274800


def test_hyperv_missing_config_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``HA_VM_NAME`` surfaces as 503, not a 500."""
    from src.hyperv_client import HyperVConfigError

    def boom() -> None:
        raise HyperVConfigError("HA_VM_NAME is not set — add the Hyper-V VM name to .env.")

    monkeypatch.setattr("app.webapp.routers.hyperv.fetch_hyperv_state", boom)
    assert client.get("/api/hyperv").status_code == 503


def test_hyperv_start_stop_invoke_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/hyperv/{start|stop}`` calls the core and returns read-back state."""
    import app.webapp.routers.hyperv as hv
    from src.hyperv_client import HyperVState

    calls: List[str] = []

    def fake_start() -> HyperVState:
        calls.append("start")
        return HyperVState(available=True, name="HA", state="running", uptime_seconds=1)

    def fake_stop() -> HyperVState:
        calls.append("stop")
        return HyperVState(available=True, name="HA", state="off", uptime_seconds=0)

    # The route resolves the action via a dict built at import time, so patch it.
    monkeypatch.setitem(hv._ACTIONS, "start", fake_start)
    monkeypatch.setitem(hv._ACTIONS, "stop", fake_stop)

    started = client.post("/api/hyperv/start")
    stopped = client.post("/api/hyperv/stop")
    assert started.status_code == 200 and started.json()["hyperv"]["state"] == "running"
    assert stopped.status_code == 200 and stopped.json()["hyperv"]["state"] == "off"
    assert calls == ["start", "stop"]


def test_hyperv_action_error_mapping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each distinct cause maps to its own status: 409/404/403, and 400 for a bad action."""
    import app.webapp.routers.hyperv as hv
    from src.hyperv_client import (
        HyperVNotFoundError,
        HyperVPermissionError,
        HyperVStateError,
    )

    def already() -> None:
        raise HyperVStateError("VM 'HA' is already running.")

    def missing() -> None:
        raise HyperVNotFoundError("VM 'HA' was not found on this host.")

    def denied() -> None:
        raise HyperVPermissionError("Insufficient Hyper-V rights.")

    monkeypatch.setitem(hv._ACTIONS, "start", already)
    assert client.post("/api/hyperv/start").status_code == 409

    monkeypatch.setitem(hv._ACTIONS, "start", missing)
    assert client.post("/api/hyperv/start").status_code == 404

    monkeypatch.setitem(hv._ACTIONS, "start", denied)
    assert client.post("/api/hyperv/start").status_code == 403

    # An unknown action never reaches the core.
    assert client.post("/api/hyperv/restart").status_code == 400
