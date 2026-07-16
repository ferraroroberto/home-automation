"""API smoke for the voice cheat sheet (issue #437).

Static, read-only reference — no device I/O to stub, so this only pins the
payload shape the PWA card reads. Content invariants live in
``tests/test_voice_commands.py``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_voice_commands_payload(client: TestClient) -> None:
    resp = client.get("/api/voice-commands")
    assert resp.status_code == 200
    body = resp.json()

    groups = body["groups"]
    assert [g["id"] for g in groups] == [
        "alarm",
        "wake-alarms",
        "locator",
        "grocery",
        "built-ins",
    ]
    assert body["count"] == sum(len(g["commands"]) for g in groups)
    assert body["count"] > 0

    phrasing = groups[0]["commands"][0]["phrasings"][0]
    assert set(phrasing) == {"lang", "wake_word", "phrases", "example"}
