"""API smoke for the RISCO alarm zone endpoints.

``GET /api/security`` serialises system + per-zone state (trouble, ac_lost);
the zone rename/hidden/trouble_ignored routes persist their overrides, and
the schedules endpoint edits the local weekly arm/disarm schedule. RISCO
cloud is never touched — the state fetch is faked throughout. Distinct from
``test_security_overrides.py`` (the "bypass after N repeats" rule store,
issue #341).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.risco_client import SecurityState, SecurityZone


def test_security_route_surfaces_trouble_and_ac_lost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/security`` serialises the AC-power flag + per-zone trouble.

    The cloud exposes no per-detector battery (low-battery was dropped in #227),
    so the surfaced system flags are ``ac_lost`` (issue #99) plus the per-zone
    generic ``trouble`` flag (issue #84). Cloud fetch is faked — no RISCO login.
    """
    state = SecurityState(
        reachable=True,
        label="Disarmed",
        mode="disarmed",
        zones=[
            SecurityZone(id=0, name="1", type=1, trouble=True),
            SecurityZone(id=4, name="Garage", type=2, trouble=False),
        ],
        ac_lost=False,
    )

    async def fake_fetch_security_state() -> SecurityState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.security.fetch_security_state", fake_fetch_security_state
    )

    resp = client.get("/api/security")
    assert resp.status_code == 200
    body = resp.json()
    # Low-battery was removed (#227); no battery_* keys should leak through.
    assert "battery_low" not in body
    assert "battery_acknowledged" not in body
    # ac_lost drives the AC-power-lost badge on the alarm-state line (issue #99).
    assert body["ac_lost"] is False
    zones = body["zones"]
    assert zones[0]["trouble"] is True
    assert zones[1]["trouble"] is False
    # Display-name override is merged per zone (None when unset).
    assert "display_name" in zones[0]


def test_security_action_tags_actor_from_automation_source_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/security/{action}`` tags ``logs/alarm.jsonl`` with the caller (issue #405).

    ``X-Automation-Source: ha`` / ``voice-pe`` map to that actor; an absent or
    unrecognized header falls back to ``webapp`` (the PWA's own fetch calls,
    which send no such header).
    """
    state = SecurityState(reachable=True, label="Armed", mode="armed")

    async def fake_control_system(action: str):
        return state

    recorded: list[str | None] = []

    async def fake_record_alarm_action(*, actor=None, **kwargs) -> None:
        recorded.append(actor)

    monkeypatch.setattr("app.webapp.routers.security.control_system", fake_control_system)
    monkeypatch.setattr("app.webapp.routers.security.note_manual_alarm_action", lambda action: None)
    monkeypatch.setattr(
        "app.webapp.routers.security.record_alarm_action", fake_record_alarm_action
    )

    assert client.post("/api/security/arm").status_code == 200
    assert client.post(
        "/api/security/arm", headers={"X-Automation-Source": "ha"}
    ).status_code == 200
    assert client.post(
        "/api/security/arm", headers={"X-Automation-Source": "voice-pe"}
    ).status_code == 200
    assert client.post(
        "/api/security/arm", headers={"X-Automation-Source": "bogus"}
    ).status_code == 200

    assert recorded == ["webapp", "ha", "voice-pe", "webapp"]


def test_security_zone_rename_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``PUT /api/security/zones/{id}/display_name`` writes the override atomically."""
    import src.security_display_names as sdn

    store = tmp_path / "security_display_names.json"
    monkeypatch.setattr(sdn, "DEFAULT_PATH", store)

    resp = client.put("/api/security/zones/4/display_name", json={"display_name": "Garage"})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "display_name": "Garage"}
    assert sdn.load_security_display_names() == {"4": "Garage"}

    # Clearing removes the entry.
    resp = client.put("/api/security/zones/4/display_name", json={"display_name": "  "})
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None
    assert sdn.load_security_display_names() == {}


def test_security_zone_hidden_persists_and_merges(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``PUT /api/security/zones/{id}/hidden`` persists, and GET merges the flag."""
    import src.security_hidden as shd

    store = tmp_path / "security_hidden.json"
    monkeypatch.setattr(shd, "DEFAULT_PATH", store)

    # Hide zone 4, then verify it round-trips on disk.
    resp = client.put("/api/security/zones/4/hidden", json={"hidden": True})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "hidden": True}
    assert shd.load_hidden_zone_ids() == {"4"}

    # GET merges the hidden flag per zone (only the hidden one is True).
    state = SecurityState(
        reachable=True,
        label="Disarmed",
        mode="disarmed",
        zones=[
            SecurityZone(id=0, name="1", type=1),
            SecurityZone(id=4, name="Garage", type=2),
        ],
    )

    async def fake_fetch_security_state() -> SecurityState:
        return state

    monkeypatch.setattr(
        "app.webapp.routers.security.fetch_security_state", fake_fetch_security_state
    )
    body = client.get("/api/security").json()
    zones = {z["id"]: z["hidden"] for z in body["zones"]}
    assert zones == {0: False, 4: True}

    # Un-hiding clears the entry.
    resp = client.put("/api/security/zones/4/hidden", json={"hidden": False})
    assert resp.status_code == 200
    assert resp.json() == {"zone_id": 4, "hidden": False}
    assert shd.load_hidden_zone_ids() == set()


def test_security_schedules_endpoint_persists_normalized_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``GET/PUT /api/security/schedules`` edits the local weekly alarm schedule list."""
    import src.security_schedules as schedules

    store = tmp_path / "security_schedules.json"
    monkeypatch.setattr(schedules, "SCHEDULES_PATH", store)

    resp = client.get("/api/security/schedules")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "count": 0, "entries": []}

    resp = client.put(
        "/api/security/schedules",
        json={
            "entries": [
                {
                    "id": "Weekend full",
                    "enabled": True,
                    "time": "22:30",
                    "days": ["sat", "sun"],
                    "action": "arm",
                },
                {
                    "id": "weekday-disarm",
                    "enabled": False,
                    "time": "07:15",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "action": "disarm",
                },
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["entries"][0] == {
        "id": "Weekend-full",
        "enabled": True,
        "time": "22:30",
        "days": ["sat", "sun"],
        "action": "arm",
    }
    assert schedules.load_security_schedules(path=store)[1].enabled is False


def test_security_zone_trouble_ignore(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Zone payload exposes trouble_ignored; PUT persists it via the store (#225)."""
    import src.security_trouble_ignore as ti
    from src.risco_client import SecurityState, SecurityZone

    monkeypatch.setattr(ti, "DEFAULT_PATH", tmp_path / "security_trouble_ignore.json")

    async def fake_state() -> SecurityState:
        return SecurityState(
            reachable=True, label="Disarmed", mode="disarmed",
            zones=[SecurityZone(id=3, name="3", trouble=True)],
        )

    monkeypatch.setattr("app.webapp.routers.security.fetch_security_state", fake_state)

    # Default: troubled but not ignored.
    body = client.get("/api/security").json()
    zone = body["zones"][0]
    assert zone["trouble"] is True and zone["trouble_ignored"] is False

    # Ignore it → persisted.
    put = client.put("/api/security/zones/3/trouble_ignored", json={"ignored": True})
    assert put.status_code == 200
    assert put.json() == {"zone_id": 3, "trouble_ignored": True}
    assert ti.load_ignored_trouble_zone_ids(tmp_path / "security_trouble_ignore.json") == {"3"}

    # Subsequent reads reflect it.
    body = client.get("/api/security").json()
    assert body["zones"][0]["trouble_ignored"] is True
