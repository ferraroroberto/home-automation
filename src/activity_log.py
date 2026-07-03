"""Modular local activity log — append-only JSONL, one file per consumer.

A tiny, reusable facility for recording "something happened, here's the result"
events to a gitignored ``logs/<consumer>.jsonl`` file, as a local alternative to
a cloud event log. Each call appends one JSON object per line (newline-delimited
JSON), stamped with a UTC ``ts`` and the ``consumer`` name if the caller didn't
provide them.

This is deliberately domain-free so any part of the app can reuse it:

    from src.activity_log import append_activity
    append_activity("alarm", {"source": "schedule", "action": "arm", "outcome": "ok"})

The ``logs/`` directory is gitignored. Writes are append-only (the natural shape
for an event log); for a *config/state* store that must be replaced atomically,
use the temp-file + ``os.replace`` pattern in ``display_names.py`` instead.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("activity_log")

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def log_path_for(consumer: str) -> Path:
    """Return the JSONL path a given consumer appends to."""

    return LOGS_DIR / f"{consumer}.jsonl"


# Events whose mere occurrence is high-severity, used to colour the activity UI.
_ALARM_EVENTS = frozenset({"intrusion", "low_battery_shutdown"})
_WARN_EVENTS = frozenset({"ac_lost", "power_lost"})


def _severity_for(record: Dict[str, Any]) -> str:
    """Best-effort severity for the unified telemetry copy of an activity event."""
    if record.get("outcome") == "error":
        return "warning"
    name = str(record.get("action") or record.get("event") or "")
    if name in _ALARM_EVENTS:
        return "alarm"
    if name in _WARN_EVENTS:
        return "warning"
    return "info"


def _mirror_to_telemetry(consumer: str, record: Dict[str, Any]) -> None:
    """Also record the event into the unified telemetry store (#289).

    The JSONL file stays the durable local trail; this adds the queryable,
    UI-surfaced copy. Gated on :func:`telemetry.default_db_ready` so it is a
    clean no-op in unit tests that never start the webapp, and never raises —
    an activity log must not break the action it records.
    """
    try:
        from src import telemetry

        if not telemetry.default_db_ready():
            return
        domain = consumer[:-9] if consumer.endswith("_triggers") else consumer
        event_type = str(record.get("action") or record.get("event") or "activity")
        telemetry.record_event(
            domain,
            event_type,
            entity_id=record.get("entity_id"),
            source=record.get("source"),
            outcome=record.get("outcome"),
            severity=_severity_for(record),
            payload=record,
        )
    except Exception as exc:  # pragma: no cover - mirror must never break logging
        logger.debug("telemetry mirror skipped (%s)", exc)


def append_activity(
    consumer: str, event: Dict[str, Any], *, path: Optional[Path] = None
) -> None:
    """Append one event to the consumer's gitignored JSONL log.

    ``ts`` (UTC ISO-8601) and ``consumer`` are filled in when absent, so the
    caller only has to supply the domain fields. Never raises on a write
    failure — an activity log must not break the action it is recording.
    """

    target = Path(path) if path is not None else log_path_for(consumer)
    record: Dict[str, Any] = dict(event)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    record.setdefault("consumer", consumer)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - disk failure is not worth crashing for
        logger.warning("⚠️ Could not append activity to %s (%s)", target, exc)

    _mirror_to_telemetry(consumer, record)
