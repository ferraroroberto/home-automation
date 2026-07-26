"""API smoke for reminders (issue #314).

``GET/PUT /api/reminders`` round-trips the reminder list; the voice endpoints
create/list/complete via a spoken phrase. The on-disk store is redirected to
``tmp_path`` so no real ``config/reminders.json`` is touched.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_reminders_round_trip(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "reminders.json")

    body = client.get("/api/reminders").json()
    assert body == {"pending_count": 0, "entries": []}

    resp = client.put(
        "/api/reminders",
        json={
            "entries": [
                {"id": "trash", "text": "take out the trash", "done": False,
                 "date": "2026-07-02", "time": "18:00"},
                {"id": "mom", "text": "call mom", "done": True},
            ]
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["pending_count"] == 1
    assert [e["id"] for e in out["entries"]] == ["trash", "mom"]
    assert out["entries"][0]["created_at"]  # server stamped it

    reread = client.get("/api/reminders").json()
    assert reread["entries"][0]["date"] == "2026-07-02"


def test_reminders_rejects_non_list(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "r.json")
    assert client.put("/api/reminders", json={"entries": "nope"}).status_code == 400


def test_voice_add_creates_and_speaks(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "reminders.json")

    resp = client.post("/api/reminders/voice", json={"phrase": "take out the trash at 6pm tomorrow"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is True
    assert out["text"] == "take out the trash"
    assert out["time"] == "18:00"
    assert out["speech"].startswith("Reminder set: take out the trash,")

    entries = client.get("/api/reminders").json()["entries"]
    assert [e["text"] for e in entries] == ["take out the trash"]


def test_voice_add_plain_reminder_has_no_due(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "reminders.json")

    out = client.post("/api/reminders/voice", json={"phrase": "call mom"}).json()
    assert out["ok"] is True
    assert out["date"] is None
    assert out["speech"] == "Reminder set: call mom."


def test_voice_add_rejects_empty_phrase(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "reminders.json")

    out = client.post("/api/reminders/voice", json={"phrase": ""}).json()
    assert out["ok"] is False
    assert "didn't catch" in out["speech"]
    assert client.get("/api/reminders").json()["pending_count"] == 0


def test_voice_list_and_complete(client: TestClient, monkeypatch, tmp_path) -> None:
    import src.reminders as reminders

    monkeypatch.setattr(reminders, "REMINDERS_PATH", tmp_path / "reminders.json")

    empty = client.get("/api/reminders/voice").json()
    assert empty == {"count": 0, "speech": "You have no pending reminders."}

    client.post("/api/reminders/voice", json={"phrase": "call mom"})
    client.post("/api/reminders/voice", json={"phrase": "take out the trash at 6pm tomorrow"})

    summary = client.get("/api/reminders/voice").json()
    assert summary["count"] == 2
    assert "2 reminders" in summary["speech"]

    # The due one (tomorrow 6pm) is completed before the undated one.
    completed = client.post("/api/reminders/voice/complete").json()
    assert completed["done"] is True
    assert "take out the trash" in completed["speech"]

    second = client.post("/api/reminders/voice/complete").json()
    assert second["done"] is True
    assert "call mom" in second["speech"]

    none_left = client.post("/api/reminders/voice/complete").json()
    assert none_left == {"done": False, "speech": "You have no pending reminders to complete."}
