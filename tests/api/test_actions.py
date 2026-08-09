"""``POST /api/actions/{action_id}`` — the generalized action alias (issue #641)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.risco_client import RiscoCommandError, SecurityState


def test_unknown_action_id_is_404(client: TestClient) -> None:
    resp = client.post("/api/actions/does_not_exist")
    assert resp.status_code == 404
    assert "does_not_exist" in resp.json()["detail"]


def test_alarm_action_wraps_control_system_with_same_side_effects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``alarm_arm`` has the same effect as ``POST /api/security/arm``: the panel
    state changes, the presence engine sees a manual action, and the alarm
    activity log gets an entry tagged with the calling actor.
    """
    import app.webapp.actions_registry as registry

    state = SecurityState(reachable=True, label="Armed", mode="armed")

    async def fake_control_system(action: str) -> SecurityState:
        assert action == "arm"
        return state

    manual_calls: List[str] = []
    recorded: List[Dict[str, Any]] = []

    async def fake_record_alarm_action(*, actor=None, outcome=None, **kwargs) -> None:
        recorded.append({"actor": actor, "outcome": outcome})

    monkeypatch.setattr(registry, "control_system", fake_control_system)
    monkeypatch.setattr(registry, "note_manual_alarm_action", manual_calls.append)
    monkeypatch.setattr(registry, "record_alarm_action", fake_record_alarm_action)

    resp = client.post(
        "/api/actions/alarm_arm", headers={"X-Automation-Source": "streamdeck"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"action_id": "alarm_arm", "ok": True, "action": "arm", "mode": "armed", "label": "Armed"}
    assert manual_calls == ["arm"]
    assert recorded == [{"actor": "streamdeck", "outcome": "ok"}]


def test_alarm_action_error_maps_to_502_and_records_error_outcome(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.actions_registry as registry

    async def fake_control_system(action: str) -> SecurityState:
        raise RiscoCommandError("panel rejected the command")

    recorded: List[Dict[str, Any]] = []

    async def fake_record_alarm_action(*, actor=None, outcome=None, **kwargs) -> None:
        recorded.append({"actor": actor, "outcome": outcome})

    monkeypatch.setattr(registry, "control_system", fake_control_system)
    monkeypatch.setattr(registry, "record_alarm_action", fake_record_alarm_action)

    resp = client.post("/api/actions/alarm_disarm")

    assert resp.status_code == 502
    assert "panel rejected" in resp.json()["detail"]
    assert recorded == [{"actor": "external", "outcome": "error"}]


@pytest.mark.parametrize("action_id,expected_on", [("plug_on", True), ("plug_off", False)])
def test_plug_action_wraps_set_switch_on_the_bound_device(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action_id: str, expected_on: bool
) -> None:
    import app.webapp.actions_registry as registry

    calls: List[tuple[str, bool]] = []

    def fake_set_switch(device_id: str, on: bool) -> Dict[str, Any]:
        calls.append((device_id, on))
        return {}

    monkeypatch.setattr(registry, "set_switch", fake_set_switch)

    resp = client.post(f"/api/actions/{action_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["switch_on"] is expected_on
    assert calls == [(registry._PLUG_DEVICE_ID, expected_on)]


@pytest.mark.parametrize("action_id,expected_mode", [("ac_on", "cool"), ("ac_off", "off")])
def test_ac_action_calls_set_hvac_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action_id: str, expected_mode: str
) -> None:
    """``climate.turn_on``/``turn_off`` 500 on this MELCloud-via-HA integration
    (found via live testing against the real device, issue #641) — the AC
    actions go through ``climate.set_hvac_mode`` with a fixed target mode
    instead.
    """
    import app.webapp.actions_registry as registry

    calls: List[tuple[str, str, str, Dict[str, Any]]] = []

    class FakeHaClient:
        def __init__(self, session) -> None:
            self.session = session

        async def call_service(self, domain: str, service: str, entity_id: str, **fields: Any) -> None:
            calls.append((domain, service, entity_id, fields))

    monkeypatch.setattr(registry, "HomeAssistantClient", FakeHaClient)

    resp = client.post(f"/api/actions/{action_id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls == [
        ("climate", "set_hvac_mode", registry._AC_CLIMATE_ENTITY, {"hvac_mode": expected_mode})
    ]


def test_action_events_are_tagged_distinctly_from_webapp_ui_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguishability requirement: a call through this endpoint records a
    ``domain="action"`` telemetry event carrying the caller's actor — a plain
    webapp-UI plug toggle (``POST /api/tuya/{id}/switch``) never writes this
    domain at all.
    """
    import app.webapp.actions_registry as registry
    import app.webapp.routers.actions as router

    monkeypatch.setattr(registry, "set_switch", lambda device_id, on: {})

    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        router.telemetry,
        "record_event",
        lambda domain, event_type, **kwargs: recorded.append(
            {"domain": domain, "event_type": event_type, **kwargs}
        ),
    )

    resp = client.post("/api/actions/plug_on", headers={"X-Automation-Source": "StreamDeck"})

    assert resp.status_code == 200
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "action"
    assert recorded[0]["event_type"] == "plug_on"
    assert recorded[0]["source"] == "streamdeck"
    assert recorded[0]["outcome"] == "ok"
