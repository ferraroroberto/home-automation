"""API smoke for ``GET``/``PUT /api/blinds/schedules`` (issue #871)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import src.blind_automation as B

    path = tmp_path / "blind_schedules.json"
    monkeypatch.setattr(B, "SCHEDULES_PATH", path)
    return path


def test_blind_schedules_round_trip(client: TestClient, store) -> None:
    assert client.get("/api/blinds/schedules").json() == {"enabled": False, "count": 0, "entries": []}
    put = client.put(
        "/api/blinds/schedules",
        json={"entries": [
            {"id": "weekend-up", "time": "07:00", "days": ["sat", "sun"], "action": "open"},
            {"id": "down", "time": "21:30", "action": "close", "presence": "away",
             "targets": ["blind-1"], "enabled": False},
        ]},
    )
    assert put.status_code == 200
    body = client.get("/api/blinds/schedules").json()
    assert body["count"] == 1  # only enabled entries count
    by_id = {e["id"]: e for e in body["entries"]}
    assert by_id["weekend-up"]["days"] == ["sat", "sun"]
    assert by_id["weekend-up"]["presence"] == "any"
    assert by_id["down"]["targets"] == ["blind-1"]
    assert store.exists()


def test_blind_schedules_reject_a_non_list(client: TestClient, store) -> None:
    response = client.put("/api/blinds/schedules", json={"entries": {"id": "x"}})
    assert response.status_code == 400
