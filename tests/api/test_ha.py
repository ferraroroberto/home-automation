from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


def _satellite(*, online: bool = True) -> Dict[str, Any]:
    return {
        "entity_id": "assist_satellite.kitchen",
        "name": "Kitchen Voice",
        "room": "Kitchen",
        "online": online,
        "state": "idle" if online else "unavailable",
        "volume": 0.75,
        "media_player": "media_player.kitchen",
    }


class FakeHaClient:
    announced: List[tuple[str, str]] = []
    online = True

    def __init__(self, session) -> None:
        self.session = session

    async def satellites(self):
        return [_satellite(online=self.online)]

    async def state(self, entity_id: str):
        return {"entity_id": entity_id, "state": "idle" if self.online else "unavailable"}

    async def announce(self, entity_id: str, message: str) -> None:
        self.announced.append((entity_id, message))


def test_get_ha_returns_ha_owned_room_and_recent_interaction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.ha as router

    monkeypatch.setattr(router, "HomeAssistantClient", FakeHaClient)
    monkeypatch.setattr(
        router.telemetry,
        "read_events",
        lambda **kwargs: [
            {
                "ts": 1784142525,
                "payload": {
                    "satellite_id": "assist_satellite.kitchen",
                    "transcript": "Turn on the light",
                },
            }
        ],
    )

    response = client.get("/api/ha")

    assert response.status_code == 200
    body = response.json()
    assert body["satellites"][0]["room"] == "Kitchen"
    assert body["satellites"][0]["volume"] == 0.75
    assert body["interactions"][0]["room"] == "Kitchen"
    assert body["interactions"][0]["transcript"] == "Turn on the light"


def test_announce_checks_online_state_and_records_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.ha as router

    FakeHaClient.online = True
    FakeHaClient.announced = []
    recorded = []
    monkeypatch.setattr(router, "HomeAssistantClient", FakeHaClient)
    monkeypatch.setattr(
        router.telemetry,
        "record_event",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    response = client.post(
        "/api/ha/satellites/assist_satellite.kitchen/announce",
        json={"text": " Dinner is ready. "},
    )

    assert response.status_code == 200
    assert FakeHaClient.announced == [("assist_satellite.kitchen", "Dinner is ready.")]
    assert recorded[0][0] == ("ha_voice", "push_to_talk")
    assert recorded[0][1]["source"] == "home-automation"
    assert recorded[0][1]["payload"]["action"] == "assist_satellite.announce"


def test_announce_distinguishes_empty_and_offline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.ha as router

    monkeypatch.setattr(router, "HomeAssistantClient", FakeHaClient)
    empty = client.post(
        "/api/ha/satellites/assist_satellite.kitchen/announce", json={"text": "  "}
    )
    assert empty.status_code == 400
    assert "nothing heard" in empty.json()["detail"]

    FakeHaClient.online = False
    offline = client.post(
        "/api/ha/satellites/assist_satellite.kitchen/announce", json={"text": "hello"}
    )
    assert offline.status_code == 409
    assert "offline" in offline.json()["detail"]
    FakeHaClient.online = True


class FakeVoiceClient:
    chunks: List[bytes] = []

    def __init__(self, session) -> None:
        self.session = session

    async def create_session(self, language=None):
        return {"session_id": "vt-1", "source": "home-automation"}

    async def send_chunk(self, session_id, content, content_type):
        self.chunks.append(content)
        return {"session_id": session_id, "raw_bytes": len(content)}

    async def finish(self, session_id, language=None):
        return {"session_id": session_id, "transcript": "hello room", "language": "en"}


def test_streaming_transcription_proxy_create_chunk_finish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.webapp.routers.ha as router

    FakeVoiceClient.chunks = []
    monkeypatch.setattr(router, "VoiceTranscriberClient", FakeVoiceClient)

    created = client.post("/api/ha/transcribe/sessions")
    chunk = client.post(
        "/api/ha/transcribe/sessions/vt-1/chunk",
        content=b"audio",
        headers={"Content-Type": "audio/webm"},
    )
    finished = client.post("/api/ha/transcribe/sessions/vt-1/finish")

    assert created.status_code == 200
    assert created.json()["source"] == "home-automation"
    assert chunk.status_code == 200 and FakeVoiceClient.chunks == [b"audio"]
    assert finished.status_code == 200
    assert finished.json()["transcript"] == "hello room"


def test_streaming_transcription_rejects_bad_session_and_large_chunk(
    client: TestClient,
) -> None:
    assert client.post("/api/ha/transcribe/sessions/not.safe/chunk", content=b"x").status_code == 400
    response = client.post(
        "/api/ha/transcribe/sessions/vt-1/chunk", content=b"x" * (2 * 1024 * 1024 + 1)
    )
    assert response.status_code == 413
