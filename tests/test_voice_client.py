from __future__ import annotations

import asyncio

from src.voice_client import VoiceTranscriberClient


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, content_type=None):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response({"session_id": "vt-1", "source": kwargs["json"]["source"]})


def test_create_session_attributes_take_to_home_automation() -> None:
    async def run() -> None:
        session = _Session()
        client = VoiceTranscriberClient(session, "https://127.0.0.1:8443")

        result = await client.create_session("en")

        assert result == {"session_id": "vt-1", "source": "home-automation"}
        method, url, kwargs = session.calls[0]
        assert method == "POST"
        assert url == "https://127.0.0.1:8443/api/sessions"
        assert kwargs["json"] == {"source": "home-automation", "language": "en"}
        assert kwargs["ssl"] is False

    asyncio.run(run())
