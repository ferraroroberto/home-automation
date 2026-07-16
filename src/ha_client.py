"""UI-free Home Assistant REST/WebSocket client for Voice PE consumption.

The webapp owns presentation and polling cadence; this module owns only the
Home Assistant transport and normalization seams.  It reuses ``HA_URL`` and
``HA_TOKEN`` from the existing config-sync workflow rather than introducing a
second credential pair.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class HaConfig:
    """Connection details for the Home Assistant API."""

    base_url: str
    token: str

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        rest = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{rest}/api/websocket"


def load_config() -> HaConfig:
    """Load the existing Home Assistant URL/token pair from ``.env``."""

    load_dotenv(PROJECT_ROOT / ".env", override=True)
    return HaConfig(
        base_url=os.getenv("HA_URL", "").strip().rstrip("/"),
        token=os.getenv("HA_TOKEN", "").strip(),
    )


class HaClientError(RuntimeError):
    """A distinct Home Assistant configuration/transport/API failure."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class HomeAssistantClient:
    """Small async client for states, services, registries, and Assist traces."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: Optional[HaConfig] = None,
    ) -> None:
        self.session = session
        self.config = config or load_config()
        if not self.config.base_url:
            raise HaClientError("HA_URL is not configured", status=503)
        if not self.config.token:
            raise HaClientError("HA_TOKEN is not configured", status=503)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.config.token}"}

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        try:
            async with self.session.request(
                method,
                f"{self.config.base_url}{path}",
                headers=self._headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 401:
                    raise HaClientError("Home Assistant rejected HA_TOKEN", status=502)
                if response.status == 404:
                    raise HaClientError("Home Assistant entity was not found", status=404)
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise HaClientError(
                        f"Home Assistant returned HTTP {response.status}: {detail}",
                        status=502,
                    )
                return await response.json(content_type=None)
        except HaClientError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HaClientError(f"Home Assistant is offline or unreachable: {exc}") from exc

    async def states(self) -> List[Dict[str, Any]]:
        """Return the full HA state list."""

        payload = await self._json("GET", "/api/states")
        return payload if isinstance(payload, list) else []

    async def state(self, entity_id: str) -> Dict[str, Any]:
        """Return one entity state without loading registries or all HA states."""

        payload = await self._json("GET", f"/api/states/{quote(entity_id, safe='.')}")
        return payload if isinstance(payload, dict) else {}

    async def announce(self, entity_id: str, message: str) -> None:
        """Speak *message* through one Assist Satellite."""

        await self._json(
            "POST",
            "/api/services/assist_satellite/announce",
            body={"entity_id": entity_id, "message": message},
        )

    async def _open_ws(self) -> aiohttp.ClientWebSocketResponse:
        try:
            ws = await self.session.ws_connect(
                self.config.websocket_url,
                timeout=aiohttp.ClientWSTimeout(ws_receive=15),
                heartbeat=30,
            )
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                await ws.close()
                raise HaClientError("Home Assistant WebSocket did not request authentication")
            await ws.send_json({"type": "auth", "access_token": self.config.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                await ws.close()
                raise HaClientError("Home Assistant rejected HA_TOKEN", status=502)
            return ws
        except HaClientError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HaClientError(f"Home Assistant is offline or unreachable: {exc}") from exc

    @staticmethod
    async def _ws_command(
        ws: aiohttp.ClientWebSocketResponse,
        message_id: int,
        command_type: str,
        **fields: Any,
    ) -> Any:
        await ws.send_json({"id": message_id, "type": command_type, **fields})
        while True:
            reply = await ws.receive_json()
            if reply.get("id") != message_id:
                continue
            if not reply.get("success"):
                error = reply.get("error") or {}
                raise HaClientError(
                    f"Home Assistant {command_type} failed: "
                    f"{error.get('message') or error.get('code') or 'unknown error'}"
                )
            return reply.get("result")

    async def satellites(self) -> List[Dict[str, Any]]:
        """Discover Assist satellites, HA-owned room names, and companion volume."""

        states = await self.states()
        state_by_id = {
            str(row.get("entity_id")): row
            for row in states
            if isinstance(row, dict) and row.get("entity_id")
        }
        ws = await self._open_ws()
        try:
            entities = await self._ws_command(ws, 1, "config/entity_registry/list")
            devices = await self._ws_command(ws, 2, "config/device_registry/list")
            areas = await self._ws_command(ws, 3, "config/area_registry/list")
        finally:
            await ws.close()

        entity_rows = entities if isinstance(entities, list) else []
        device_rows = devices if isinstance(devices, list) else []
        area_rows = areas if isinstance(areas, list) else []
        entity_registry = {
            str(row.get("entity_id")): row for row in entity_rows if row.get("entity_id")
        }
        device_registry = {str(row.get("id")): row for row in device_rows if row.get("id")}
        area_names = {
            str(row.get("area_id") or row.get("id")): str(row.get("name") or "")
            for row in area_rows
            if row.get("area_id") or row.get("id")
        }

        media_by_device: Dict[str, Dict[str, Any]] = {}
        for entity in entity_rows:
            entity_id = str(entity.get("entity_id") or "")
            device_id = str(entity.get("device_id") or "")
            if entity_id.startswith("media_player.") and device_id and entity_id in state_by_id:
                media_by_device.setdefault(device_id, state_by_id[entity_id])

        result: List[Dict[str, Any]] = []
        for entity_id, state in state_by_id.items():
            if not entity_id.startswith("assist_satellite."):
                continue
            registry = entity_registry.get(entity_id, {})
            device_id = str(registry.get("device_id") or "")
            device = device_registry.get(device_id, {})
            area_id = str(registry.get("area_id") or device.get("area_id") or "")
            attrs = state.get("attributes") or {}
            media = media_by_device.get(device_id, {})
            media_attrs = media.get("attributes") or {}
            raw_state = str(state.get("state") or "unknown")
            result.append(
                {
                    "entity_id": entity_id,
                    "name": str(
                        attrs.get("friendly_name")
                        or registry.get("name")
                        or registry.get("original_name")
                        or entity_id.split(".", 1)[-1].replace("_", " ").title()
                    ),
                    "room": area_names.get(area_id) or "Unassigned",
                    "online": raw_state not in {"unknown", "unavailable"},
                    "state": raw_state,
                    "volume": media_attrs.get("volume_level"),
                    "media_player": media.get("entity_id"),
                }
            )
        return sorted(result, key=lambda row: (row["room"].casefold(), row["name"].casefold()))

    async def pipeline_runs(
        self, seen_run_ids: Optional[set[str]] = None
    ) -> List[Dict[str, Any]]:
        """Return HA's bounded recent Assist debug traces, including local intent debug."""

        ws = await self._open_ws()
        message_id = 1
        try:
            listed = await self._ws_command(ws, message_id, "assist_pipeline/pipeline/list")
            pipelines = (listed or {}).get("pipelines") or []
            runs: List[Dict[str, Any]] = []
            for pipeline in pipelines:
                message_id += 1
                debug = await self._ws_command(
                    ws,
                    message_id,
                    "assist_pipeline/pipeline_debug/list",
                    pipeline_id=pipeline.get("id"),
                )
                for summary in (debug or {}).get("pipeline_runs") or []:
                    run_id = summary.get("pipeline_run_id")
                    if not run_id or (seen_run_ids is not None and run_id in seen_run_ids):
                        continue
                    message_id += 1
                    detail = await self._ws_command(
                        ws,
                        message_id,
                        "assist_pipeline/pipeline_debug/get",
                        pipeline_id=pipeline.get("id"),
                        pipeline_run_id=run_id,
                    )
                    row: Dict[str, Any] = {
                        "pipeline_id": pipeline.get("id"),
                        "pipeline_name": pipeline.get("name"),
                        "run_id": run_id,
                        "timestamp": summary.get("timestamp"),
                        "events": (detail or {}).get("events") or [],
                    }
                    intent_start = next(
                        (event for event in row["events"] if event.get("type") == "intent-start"),
                        None,
                    )
                    intent_end = next(
                        (event for event in row["events"] if event.get("type") == "intent-end"),
                        None,
                    )
                    event_types = {event.get("type") for event in row["events"]}
                    complete = bool({"run-end", "error"} & event_types)
                    if complete and intent_start and intent_end and (intent_end.get("data") or {}).get("processed_locally"):
                        start_data = intent_start.get("data") or {}
                        sentence = str(start_data.get("intent_input") or "").strip()
                        if sentence:
                            message_id += 1
                            debug_fields: Dict[str, Any] = {"sentences": [sentence]}
                            if start_data.get("language"):
                                debug_fields["language"] = start_data["language"]
                            debug_result = await self._ws_command(
                                ws,
                                message_id,
                                "conversation/agent/homeassistant/debug",
                                **debug_fields,
                            )
                            row["local_debug"] = debug_result
                    runs.append(row)
            return runs
        finally:
            await ws.close()


def normalize_pipeline_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse one raw HA trace into the compact interaction-log contract."""

    events = run.get("events") or []
    by_type = {event.get("type"): event for event in events if event.get("type")}
    run_start = (by_type.get("run-start") or {}).get("data") or {}
    stt = (by_type.get("stt-end") or {}).get("data") or {}
    intent_start = (by_type.get("intent-start") or {}).get("data") or {}
    intent_end = (by_type.get("intent-end") or {}).get("data") or {}
    tts = (by_type.get("tts-start") or {}).get("data") or {}
    error = next((event for event in events if event.get("type") == "error"), None)

    transcript = str((stt.get("stt_output") or {}).get("text") or "").strip()
    response = (intent_end.get("intent_output") or {}).get("response") or {}
    spoken = str(
        ((response.get("speech") or {}).get("plain") or {}).get("speech")
        or tts.get("tts_input")
        or ""
    ).strip()
    processed_locally = intent_end.get("processed_locally") is True
    debug_rows = (run.get("local_debug") or {}).get("results") or []
    debug_match = next(
        (row for row in debug_rows if isinstance(row, dict) and row.get("match")),
        None,
    )
    intent_name = str(((debug_match or {}).get("intent") or {}).get("name") or "")
    engine = str(intent_start.get("engine") or "")

    response_data = response.get("data") or {}
    targets: List[str] = []
    for bucket in ("success", "failed"):
        bucket_value = response_data.get(bucket) or []
        if isinstance(bucket_value, dict):
            bucket_value = list(bucket_value.values())
        for target in bucket_value:
            if isinstance(target, dict):
                label = target.get("name") or target.get("id") or target.get("entity_id")
                if label:
                    targets.append(str(label))
            elif target:
                targets.append(str(target))
    action = ", ".join(targets) or intent_name or str(response.get("response_type") or "")
    timestamp = str(run.get("timestamp") or "")
    if not timestamp and events:
        timestamp = str(events[0].get("timestamp") or "")

    return {
        "run_id": str(run.get("run_id") or ""),
        "timestamp": timestamp,
        "pipeline": str(run.get("pipeline_name") or run_start.get("pipeline") or ""),
        "language": str(run_start.get("language") or intent_start.get("language") or ""),
        "satellite_id": str(run_start.get("satellite_id") or intent_start.get("satellite_id") or ""),
        "transcript": transcript,
        "intent_kind": "local" if processed_locally else "fallback",
        "intent": intent_name or (engine if not processed_locally else "local"),
        "action": action,
        "spoken_response": spoken,
        "outcome": "error" if error else "ok",
        "error": (error or {}).get("data"),
    }


def timestamp_epoch(value: str) -> Optional[int]:
    """Parse an HA ISO timestamp for telemetry storage, or return ``None``."""

    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None
