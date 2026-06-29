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
