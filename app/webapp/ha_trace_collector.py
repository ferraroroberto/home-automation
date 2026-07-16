"""Best-effort, bounded Home Assistant Assist-pipeline trace ingestion."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Deque, Optional, Set

import aiohttp

from src import telemetry
from src.ha_client import (
    HaClientError,
    HomeAssistantClient,
    load_config,
    normalize_pipeline_run,
    timestamp_epoch,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 15
SEEN_RUN_LIMIT = 256


class _SeenRuns:
    """Bounded ordered set so a long-running webapp cannot grow forever."""

    def __init__(self) -> None:
        self._queue: Deque[str] = deque()
        self.ids: Set[str] = set()

    def add(self, run_id: str) -> None:
        if not run_id or run_id in self.ids:
            return
        if len(self._queue) >= SEEN_RUN_LIMIT:
            self.ids.discard(self._queue.popleft())
        self._queue.append(run_id)
        self.ids.add(run_id)


def _seed_seen(seen: _SeenRuns) -> None:
    """Restore recent run IDs from SQLite so a restart does not duplicate rows."""

    try:
        for event in reversed(telemetry.read_events(domain="ha_voice", limit=SEEN_RUN_LIMIT)):
            payload = event.get("payload") or {}
            seen.add(str(payload.get("run_id") or ""))
    except Exception as exc:  # noqa: BLE001 — collection remains best effort
        logger.debug("HA trace dedupe seed unavailable: %s", exc)


async def _record_new_runs(client: HomeAssistantClient, seen: _SeenRuns) -> int:
    rows = await client.pipeline_runs(seen.ids)
    recorded = 0
    for raw in sorted(rows, key=lambda row: str(row.get("timestamp") or "")):
        event_types = {event.get("type") for event in raw.get("events") or []}
        # An in-flight trace remains unseen so the next poll captures its final
        # transcript/intent/response instead of permanently logging a partial.
        if not ({"run-end", "error"} & event_types):
            continue
        interaction = normalize_pipeline_run(raw)
        run_id = interaction["run_id"]
        if not run_id or run_id in seen.ids:
            continue
        detail = interaction.get("transcript") or interaction.get("spoken_response")
        payload = dict(interaction)
        if detail:
            payload["detail"] = detail
        await asyncio.to_thread(
            telemetry.record_event,
            "ha_voice",
            "interaction",
            entity_id=interaction.get("satellite_id") or None,
            source="home_assistant",
            outcome=interaction.get("outcome") or "ok",
            severity="warning" if interaction.get("outcome") == "error" else "info",
            payload=payload,
            ts=timestamp_epoch(interaction.get("timestamp") or ""),
        )
        seen.add(run_id)
        recorded += 1
    return recorded


async def _collector_loop() -> None:
    seen = _SeenRuns()
    await asyncio.to_thread(_seed_seen, seen)
    timeout = aiohttp.ClientTimeout(total=30)
    last_error: Optional[str] = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = HomeAssistantClient(session)
        while True:
            try:
                count = await _record_new_runs(client, seen)
                if last_error is not None:
                    logger.info("ℹ️  Home Assistant trace collection recovered")
                    last_error = None
                if count:
                    logger.info("ℹ️  Recorded %s Home Assistant voice interaction(s)", count)
            except HaClientError as exc:
                message = str(exc)
                if message != last_error:
                    logger.info("ℹ️  Home Assistant trace collection unavailable: %s", exc)
                    last_error = message
            except Exception as exc:  # noqa: BLE001 — background task must survive
                message = str(exc)
                if message != last_error:
                    logger.warning("⚠️  Home Assistant trace collection failed: %s", exc)
                    last_error = message
            await asyncio.sleep(POLL_SECONDS)


def start_ha_trace_collector() -> Optional[asyncio.Task]:
    """Start collection when configured; otherwise leave the webapp unaffected."""

    if os.getenv("HA_TRACE_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return None
    config = load_config()
    if not config.base_url or not config.token:
        logger.info("ℹ️  HA trace collection disabled (HA_URL/HA_TOKEN not configured)")
        return None
    return asyncio.create_task(_collector_loop(), name="ha-trace-collector")
