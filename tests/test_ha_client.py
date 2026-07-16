from __future__ import annotations

import asyncio

from src.ha_client import HaConfig, HomeAssistantClient, normalize_pipeline_run, timestamp_epoch


def test_satellite_discovery_uses_ha_area_and_companion_volume(monkeypatch) -> None:
    class Ws:
        async def close(self):
            return None

    client = HomeAssistantClient(
        object(), HaConfig(base_url="http://ha.test:8123", token="token")
    )

    async def states():
        return [
            {
                "entity_id": "assist_satellite.voice_kitchen",
                "state": "idle",
                "attributes": {"friendly_name": "Kitchen Voice"},
            },
            {
                "entity_id": "media_player.voice_kitchen",
                "state": "idle",
                "attributes": {"volume_level": 0.73},
            },
        ]

    async def open_ws():
        return Ws()

    async def command(_ws, _message_id, command_type, **_fields):
        return {
            "config/entity_registry/list": [
                {
                    "entity_id": "assist_satellite.voice_kitchen",
                    "device_id": "device-1",
                    "area_id": None,
                },
                {
                    "entity_id": "media_player.voice_kitchen",
                    "device_id": "device-1",
                },
            ],
            "config/device_registry/list": [
                {"id": "device-1", "area_id": "kitchen"}
            ],
            "config/area_registry/list": [
                {"area_id": "kitchen", "name": "Kitchen"}
            ],
        }[command_type]

    monkeypatch.setattr(client, "states", states)
    monkeypatch.setattr(client, "_open_ws", open_ws)
    monkeypatch.setattr(client, "_ws_command", command)

    rows = asyncio.run(client.satellites())

    assert rows == [
        {
            "entity_id": "assist_satellite.voice_kitchen",
            "name": "Kitchen Voice",
            "room": "Kitchen",
            "online": True,
            "state": "idle",
            "volume": 0.73,
            "media_player": "media_player.voice_kitchen",
        }
    ]


def test_normalize_pipeline_run_keeps_complete_voice_interaction() -> None:
    row = normalize_pipeline_run(
        {
            "run_id": "run-1",
            "timestamp": "2026-07-15T19:08:45+00:00",
            "pipeline_name": "Focused local assistant",
            "local_debug": {
                "results": [{"match": True, "intent": {"name": "Locate"}}]
            },
            "events": [
                {
                    "type": "run-start",
                    "data": {
                        "language": "en",
                        "satellite_id": "assist_satellite.kitchen",
                    },
                },
                {"type": "stt-end", "data": {"stt_output": {"text": " Where is mom?\n"}}},
                {"type": "intent-start", "data": {"engine": "conversation.home_assistant"}},
                {
                    "type": "intent-end",
                    "data": {
                        "processed_locally": True,
                        "intent_output": {
                            "response": {
                                "response_type": "action_done",
                                "speech": {"plain": {"speech": "Mom is home."}},
                                "data": {"success": []},
                            }
                        },
                    },
                },
                {"type": "tts-start", "data": {"tts_input": "Mom is home."}},
                {"type": "run-end", "data": None},
            ],
        }
    )

    assert row == {
        "run_id": "run-1",
        "timestamp": "2026-07-15T19:08:45+00:00",
        "pipeline": "Focused local assistant",
        "language": "en",
        "satellite_id": "assist_satellite.kitchen",
        "transcript": "Where is mom?",
        "intent_kind": "local",
        "intent": "Locate",
        "action": "Locate",
        "spoken_response": "Mom is home.",
        "outcome": "ok",
        "error": None,
    }


def test_normalize_pipeline_run_labels_fallback_and_error() -> None:
    row = normalize_pipeline_run(
        {
            "run_id": "run-2",
            "pipeline_name": "Home Assistant",
            "events": [
                {"type": "stt-end", "data": {"stt_output": {"text": "hello"}}},
                {"type": "intent-start", "data": {"engine": "conversation.external"}},
                {"type": "intent-end", "data": {"processed_locally": False}},
                {"type": "error", "data": {"code": "intent-failed"}},
            ],
        }
    )

    assert row["intent_kind"] == "fallback"
    assert row["intent"] == "conversation.external"
    assert row["outcome"] == "error"
    assert row["error"] == {"code": "intent-failed"}


def test_timestamp_epoch_accepts_ha_iso_and_rejects_bad_value() -> None:
    assert timestamp_epoch("2026-07-15T19:08:45+00:00") == 1784142525
    assert timestamp_epoch("not-a-time") is None
