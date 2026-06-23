"""Background presence -> alarm automation consumer."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from src.presence_engine import (
    append_trigger_log,
    evaluate_alarm_decision,
    load_automation_config,
    load_people,
    mark_decision_applied,
)
from src.presence_hidden import load_hidden_presence_ids
from src.push_notifications import send_push
from src.risco_client import control_system, fetch_security_state

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


async def tick() -> None:
    """Evaluate one presence transition and apply at most one alarm action."""

    config = load_automation_config()
    if not config.enabled:
        return
    hidden = load_hidden_presence_ids()
    people = [p for pid, p in load_people().items() if pid not in hidden]
    if not people:
        return

    security = await fetch_security_state()
    decision = evaluate_alarm_decision(
        people,
        security_mode=security.mode,
        config=config,
        at=datetime.now(timezone.utc),
    )
    if decision is None:
        return

    outcome = "started"
    try:
        updated = await control_system(decision.action)
        outcome = updated.mode
        mark_decision_applied(decision, outcome)
        logger.info("✅ Presence automation %s -> %s", decision.reason, decision.action)
        send_push("Presence automation", f"{decision.reason}: {decision.action}")
    except Exception as exc:  # noqa: BLE001
        outcome = f"error: {exc}"
        logger.warning("⚠️ Presence automation action failed: %s", exc)
    finally:
        append_trigger_log(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "consumer": "alarm",
                "event": decision.kind,
                "action": decision.action,
                "reason": decision.reason,
                "transition_at": decision.transition_at.isoformat(),
                "outcome": outcome,
            }
        )


async def _run(interval_s: int) -> None:
    logger.info("🛡️ Presence alarm automation started (poll %ds)", interval_s)
    try:
        while True:
            try:
                await tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️ Presence automation tick failed: %s", exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("🛑 Presence alarm automation stopped")
        raise


def start_presence_automation() -> Optional[asyncio.Task]:
    """Start the presence automation task; config defaults make it a no-op."""

    load_dotenv(override=True)
    if not _env_bool("PRESENCE_AUTOMATION_ENGINE_ENABLED", True):
        logger.info("ℹ️ Presence automation engine disabled")
        return None
    interval_s = max(5, _env_int("PRESENCE_AUTOMATION_POLL_INTERVAL_S", 10))
    return asyncio.create_task(_run(interval_s), name="presence-automation")
