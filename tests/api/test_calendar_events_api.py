"""API smoke for voice-created calendar events (issue #313).

``POST /api/calendar/voice`` never touches the real Google Calendar API in
tests — ``insert_event`` is monkeypatched to a fake, matching this repo's
"cloud fetchers are monkeypatched" convention for MELCloud/SMA/Tuya/Risco.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.calendar_write import CalendarWriteError


def test_voice_add_creates_event_and_speaks(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.calendar_events as calendar_events

    calls = []

    def _fake_insert_event(body, calendar_id="primary", client=None):
        calls.append(body)
        return {"id": "evt1", "htmlLink": "https://calendar.google.com/event?eid=evt1"}

    monkeypatch.setattr(calendar_events, "insert_event", _fake_insert_event)

    resp = client.post(
        "/api/calendar/voice",
        json={"phrase": "dentist appointment to my calendar tomorrow at 3pm"},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is True
    assert out["id"] == "evt1"
    assert out["summary"] == "dentist appointment"
    assert out["html_link"] == "https://calendar.google.com/event?eid=evt1"
    assert "Added dentist appointment," in out["speech"]

    assert len(calls) == 1
    assert calls[0]["summary"] == "dentist appointment"
    assert "dateTime" in calls[0]["start"]


def test_voice_add_rejects_phrase_with_no_date_and_never_calls_insert(
    client: TestClient, monkeypatch
) -> None:
    import app.webapp.routers.calendar_events as calendar_events

    calls = []
    monkeypatch.setattr(
        calendar_events, "insert_event", lambda *a, **k: calls.append(1) or {}
    )

    resp = client.post("/api/calendar/voice", json={"phrase": "dentist appointment"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is False
    assert "at least a date" in out["speech"]
    assert calls == []


def test_voice_add_handles_calendar_write_failure_gracefully(
    client: TestClient, monkeypatch
) -> None:
    import app.webapp.routers.calendar_events as calendar_events

    def _raise(*args, **kwargs):
        raise CalendarWriteError("no token")

    monkeypatch.setattr(calendar_events, "insert_event", _raise)

    resp = client.post(
        "/api/calendar/voice",
        json={"phrase": "dentist appointment tomorrow at 3pm"},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["ok"] is False
    assert "couldn't reach your calendar" in out["speech"]


def test_voice_add_all_day_event(client: TestClient, monkeypatch) -> None:
    import app.webapp.routers.calendar_events as calendar_events

    calls = []

    def _fake_insert_event(body, calendar_id="primary", client=None):
        calls.append(body)
        return {"id": "evt2", "htmlLink": "https://calendar.google.com/event?eid=evt2"}

    monkeypatch.setattr(calendar_events, "insert_event", _fake_insert_event)

    resp = client.post(
        "/api/calendar/voice",
        json={"phrase": "team offsite to my calendar on friday"},
    )
    out = resp.json()
    assert out["ok"] is True
    assert "all day" in out["speech"]
    assert "date" in calls[0]["start"]
