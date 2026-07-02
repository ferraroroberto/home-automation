"""API smoke for wake alarms + app-native timers (issue #304).

``GET/PUT /api/wake-alarms`` round-trips the alarm list; ``.../test`` and
``.../dismiss`` drive the ringing state. ``/api/wake-timers`` is the separate
in-memory countdown pool. The on-disk alarm store is redirected to
``tmp_path`` so no real ``config/wake_alarms.json`` is touched; the timer
store is a process-global in-memory dict, so tests clean up after themselves.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_wake_alarms_round_trip(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "wake_alarms.json")

    body = client.get("/api/wake-alarms").json()
    assert body == {"enabled": False, "count": 0, "entries": []}

    resp = client.put(
        "/api/wake-alarms",
        json={
            "entries": [
                {"id": "wakeup", "label": "Wake up", "enabled": True, "time": "07:00",
                 "days": ["mon", "tue", "wed", "thu", "fri"]},
                {"id": "flight", "label": "Airport", "enabled": True, "time": "05:30",
                 "days": [], "date": "2026-08-12"},
            ]
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["count"] == 2
    assert [e["id"] for e in out["entries"]] == ["wakeup", "flight"]
    assert out["entries"][0]["ringing"] is False

    reread = client.get("/api/wake-alarms").json()
    assert reread["entries"][1]["date"] == "2026-08-12"


def test_wake_alarms_rejects_non_list(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "w.json")
    assert client.put("/api/wake-alarms", json={"entries": "nope"}).status_code == 400


def test_wake_alarm_test_and_dismiss(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "wake_alarms.json")
    client.put("/api/wake-alarms", json={"entries": [
        {"id": "test-me", "label": "Testing", "enabled": True, "time": "07:00"},
    ]})

    assert client.post("/api/wake-alarms/unknown/test").status_code == 404

    resp = client.post("/api/wake-alarms/test-me/test")
    assert resp.status_code == 200
    assert resp.json() == {"id": "test-me", "ringing": True}
    assert client.get("/api/wake-alarms").json()["entries"][0]["ringing"] is True

    resp = client.post("/api/wake-alarms/test-me/dismiss")
    assert resp.json() == {"id": "test-me", "ringing": False, "dismissed": True}
    assert client.get("/api/wake-alarms").json()["entries"][0]["ringing"] is False


def test_wake_timers_lifecycle(client: TestClient) -> None:
    created = client.post("/api/wake-timers", json={"label": "Pasta", "seconds": 300})
    assert created.status_code == 200
    timer = created.json()
    assert timer["seconds"] == 300
    assert timer["ringing"] is False

    try:
        listed = client.get("/api/wake-timers").json()
        assert [t["id"] for t in listed["timers"]] == [timer["id"]]
    finally:
        cancelled = client.delete("/api/wake-timers/" + timer["id"])
        assert cancelled.json() == {"id": timer["id"], "cancelled": True}

    assert client.get("/api/wake-timers").json() == {"timers": []}


def test_wake_timers_rejects_non_positive_seconds(client: TestClient) -> None:
    resp = client.post("/api/wake-timers", json={"label": "x", "seconds": 0})
    assert resp.status_code == 422


def test_voice_set_creates_and_speaks(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "wake_alarms.json")

    resp = client.post("/api/wake-alarms/voice", json={"phrase": "7 am on weekdays"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is True
    assert out["time"] == "07:00"
    assert out["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert out["speech"] == "Wake alarm set for 7 AM on weekdays."

    # It landed in the persisted list.
    entries = client.get("/api/wake-alarms").json()["entries"]
    assert [e["time"] for e in entries] == ["07:00"]


def test_voice_set_rejects_timeless_phrase(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "wake_alarms.json")

    out = client.post("/api/wake-alarms/voice", json={"phrase": "banana"}).json()
    assert out["ok"] is False
    assert "didn't catch a time" in out["speech"]
    assert client.get("/api/wake-alarms").json()["count"] == 0


def test_voice_list_and_cancel(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.wake_alarms as wake_alarms

    monkeypatch.setattr(wake_alarms, "WAKE_ALARMS_PATH", tmp_path / "wake_alarms.json")

    empty = client.get("/api/wake-alarms/voice").json()
    assert empty == {"count": 0, "speech": "You have no wake alarms set."}

    client.post("/api/wake-alarms/voice", json={"phrase": "7 am on weekdays"})
    summary = client.get("/api/wake-alarms/voice").json()
    assert summary["count"] == 1
    assert "1 wake alarm" in summary["speech"]

    cancelled = client.post("/api/wake-alarms/voice/cancel").json()
    assert cancelled["cancelled"] is True
    assert cancelled["speech"] == "Cancelled your wake alarm for 7 AM on weekdays."

    again = client.post("/api/wake-alarms/voice/cancel").json()
    assert again == {"cancelled": False, "speech": "You have no wake alarms to cancel."}
