"""``GET /api/scenes`` and ``POST /api/scenes/{id}`` — every device call faked, nothing actuates."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from src.devices.blind_automation import BlindOutcome
from src.devices.melcloud_client import DeviceInfo
from src.security.risco_client import RiscoCommandError, SecurityState


class _Calls:
    def __init__(self) -> None:
        self.order: List[str] = []
        self.blinds: List[str] = []
        self.settled: List[str] = []
        self.ac_off: List[str] = []
        self.alarm: List[Dict[str, Optional[str]]] = []


def _unit(unit_id: str, power: bool) -> DeviceInfo:
    return DeviceInfo(
        unit_id=unit_id, name=unit_id, building="Fixture", power=power,
        operation_mode="Cool", room_temperature=24.0, set_temperature=24.0, fan_speed="Auto",
    )


@pytest.fixture
def calls(tmp_path, monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Fake blinds, AC units and alarm panel, with the default scenes."""
    import app.webapp.routers.scenes as scenes
    import src.devices.home_scenes as store

    monkeypatch.setattr(store, "HOME_SCENES_PATH", tmp_path / "home_scenes.json")
    rec = _Calls()

    async def fake_move_blinds(action, device_ids=None):
        rec.order.append("blinds")
        rec.blinds.append(action)
        return [BlindOutcome("b1", True), BlindOutcome("b2", True)]

    async def fake_settle(action, outcomes):
        rec.settled.append(action)

    async def fake_fetch_devices():
        return [_unit("u-on", True), _unit("u-off", False), _unit("u-on-2", True)]

    async def fake_set_device_state(unit_id, **kwargs):
        rec.order.append("ac")
        assert kwargs == {"power": False}
        rec.ac_off.append(unit_id)
        return _unit(unit_id, False)

    async def fake_alarm(action, actor=None):
        rec.order.append("alarm")
        rec.alarm.append({"action": action, "actor": actor})
        return SecurityState(reachable=True, label="Armed", mode="armed")

    monkeypatch.setattr(scenes, "move_blinds", fake_move_blinds)
    monkeypatch.setattr(scenes, "settle_manual_move", fake_settle)
    monkeypatch.setattr("app.webapp.routers.units.fetch_devices", fake_fetch_devices)
    monkeypatch.setattr(scenes, "set_device_state", fake_set_device_state)
    monkeypatch.setattr(scenes, "perform_manual_alarm_action", fake_alarm)
    return rec


def test_list_scenes_reports_each_scene_steps(client: TestClient, calls: _Calls) -> None:
    resp = client.get("/api/scenes")
    assert resp.status_code == 200
    assert resp.json() == {
        "scenes": [
            {"id": "leave", "label": "Leave", "steps": ["Blinds down", "AC off", "Alarm full"]},
            {"id": "arrive", "label": "Arrive", "steps": ["Blinds up", "Alarm off"]},
            {"id": "night", "label": "Night", "steps": ["Blinds down", "Alarm perimeter"]},
        ]
    }


def test_leave_closes_blinds_switches_running_units_off_then_arms(client: TestClient, calls: _Calls) -> None:
    resp = client.post("/api/scenes/leave")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert [s["kind"] for s in body["steps"]] == ["blinds", "ac", "alarm"]
    assert calls.blinds == ["close"] and calls.settled == ["close"]
    assert sorted(calls.ac_off) == ["u-on", "u-on-2"]  # the unit already off is left alone
    assert calls.alarm == [{"action": "arm", "actor": "scene:leave"}]
    assert calls.order[-1] == "alarm"  # armed only after the house is closed up


def test_arrive_leaves_the_ac_alone(client: TestClient, calls: _Calls) -> None:
    resp = client.post("/api/scenes/arrive")
    assert resp.status_code == 200
    assert calls.blinds == ["open"]
    assert calls.ac_off == []
    assert calls.alarm == [{"action": "disarm", "actor": "scene:arrive"}]


def test_a_failed_step_does_not_stop_the_others(
    client: TestClient, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.scenes as scenes

    async def boom(action, actor=None):
        raise RiscoCommandError("panel unreachable")

    monkeypatch.setattr(scenes, "perform_manual_alarm_action", boom)
    resp = client.post("/api/scenes/leave")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    by_kind: Dict[str, Any] = {s["kind"]: s for s in body["steps"]}
    assert by_kind["blinds"]["ok"] and by_kind["ac"]["ok"]
    assert by_kind["alarm"]["ok"] is False and "panel unreachable" in by_kind["alarm"]["error"]


def test_partial_blind_failure_is_ok_with_an_error(
    client: TestClient, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.scenes as scenes

    async def half(action, device_ids=None):
        return [BlindOutcome("b1", True), BlindOutcome("b2", False, error="timeout")]

    monkeypatch.setattr(scenes, "move_blinds", half)
    body = client.post("/api/scenes/night").json()
    blinds = body["steps"][0]
    assert blinds["ok"] is True and blinds["failed"] == 1 and "1 of 2" in blinds["error"]
    assert body["ok"] is False


def test_every_step_failing_is_a_502(
    client: TestClient, calls: _Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.scenes as scenes

    async def no_blinds(action, device_ids=None):
        return [BlindOutcome("b1", False, error="offline")]

    async def no_alarm(action, actor=None):
        raise RiscoCommandError("panel unreachable")

    monkeypatch.setattr(scenes, "move_blinds", no_blinds)
    monkeypatch.setattr(scenes, "perform_manual_alarm_action", no_alarm)
    resp = client.post("/api/scenes/night")
    assert resp.status_code == 502
    assert "Night" in resp.json()["detail"]


def test_unknown_scene_is_404(client: TestClient, calls: _Calls) -> None:
    assert client.post("/api/scenes/party").status_code == 404
    assert calls.order == []


def test_config_override_changes_what_runs(client: TestClient, calls: _Calls, tmp_path) -> None:
    (tmp_path / "home_scenes.json").write_text('{"night": {"blinds": null, "alarm": "partial"}}', encoding="utf-8")
    resp = client.post("/api/scenes/night")
    assert resp.status_code == 200
    assert calls.blinds == []
    assert calls.alarm == [{"action": "partial", "actor": "scene:night"}]
