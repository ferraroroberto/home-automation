"""Unified telemetry store (SQLite) — the standard event + reading substrate.

This module is the recorder/reader core for the home-automation telemetry model
(design: issue #283). It owns a single WAL-mode SQLite file under the gitignored
runtime area (``webapp/telemetry.sqlite3``) holding two narrow, append-mostly
tables:

* ``readings`` — one row per ``(entity, metric)`` observation. Adding a new
  device type or metric is a new *row*, never a new *column* — so the schema
  never needs an ``ALTER TABLE``. A JSON1 ``attrs`` sidecar carries anything
  rare that doesn't deserve its own column yet.
* ``events`` — one row per discrete event/trigger (arm/disarm, plug toggle,
  power loss, schedule firing, …), with a JSON1 ``payload`` for arbitrary
  detail. This is the unified successor to the scattered write-only
  ``logs/*.jsonl`` trail and the otherwise-ephemeral RISCO event feed.

**Asleep is not zero.** A metric with no real numeric reading stores
``value_num = NULL`` (never 0), mirroring the convention in
:mod:`src.energy_history` — a gap is a gap, not a misleading 0.

UI-free by contract: shared by the samplers (writers) and the activity API
(readers). Never imports the UI. Storage is hidden behind this thin
recorder/reader interface so a future SQLite→Postgres move is a backend swap,
not a rewrite.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger("telemetry")

# Default DB location: the repo's gitignored runtime area, next to the other
# SQLite stores (energy_history, network_history).
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "webapp" / "telemetry.sqlite3"

_HOUR = 3600


@dataclass(frozen=True)
class TelemetryConfig:
    """Retention knobs, loaded from ``.env`` (all optional).

    Raw ``readings`` are kept for a short, bounded window; discrete ``events``
    are far rarer and human-meaningful, so they are kept much longer.
    """

    readings_retention_days: int = 7
    events_retention_days: int = 400

    @property
    def readings_retention_seconds(self) -> int:
        return self.readings_retention_days * 24 * _HOUR

    @property
    def events_retention_seconds(self) -> int:
        return self.events_retention_days * 24 * _HOUR


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


def load_telemetry_config() -> TelemetryConfig:
    """Read the telemetry retention knobs from ``.env`` (graceful defaults)."""
    load_dotenv(override=True)
    return TelemetryConfig(
        readings_retention_days=max(1, _env_int("TELEMETRY_READINGS_RETENTION_DAYS", 7)),
        events_retention_days=max(1, _env_int("TELEMETRY_EVENTS_RETENTION_DAYS", 400)),
    )


# --------------------------------------------------------------- reading row
@dataclass
class Reading:
    """One ``(entity, metric)`` observation queued for the ``readings`` table.

    Numeric metrics use :attr:`value_num`; categorical ones (mode, state) use
    :attr:`value_txt`. Leave :attr:`value_num` ``None`` for an absent numeric
    reading — it is stored as ``NULL``, never coerced to 0 (asleep ≠ zero).
    :attr:`attrs` is a free-form dict persisted to the JSON1 sidecar.
    """

    domain: str
    entity_id: str
    metric: str
    value_num: Optional[float] = None
    value_txt: Optional[str] = None
    unit: Optional[str] = None
    quality: Optional[str] = None
    attrs: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------- connection
@contextmanager
def _connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (concurrent sampler write + API read)."""
    target = Path(path) if path is not None else DEFAULT_DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def init_db(path: Optional[Path] = None) -> None:
    """Create the tables and indexes if they do not exist (idempotent)."""
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                ts        INTEGER NOT NULL,
                domain    TEXT    NOT NULL,
                entity_id TEXT    NOT NULL,
                metric    TEXT    NOT NULL,
                value_num REAL,
                value_txt TEXT,
                unit      TEXT,
                quality   TEXT,
                attrs     TEXT
            );
            CREATE INDEX IF NOT EXISTS readings_q
                ON readings(domain, entity_id, metric, ts);

            CREATE TABLE IF NOT EXISTS events (
                ts         INTEGER NOT NULL,
                domain     TEXT    NOT NULL,
                entity_id  TEXT,
                event_type TEXT    NOT NULL,
                source     TEXT,
                outcome    TEXT,
                severity   TEXT,
                payload    TEXT
            );
            CREATE INDEX IF NOT EXISTS events_q ON events(domain, ts);
            """
        )
        conn.commit()


def _dumps(value: Optional[Dict[str, Any]]) -> Optional[str]:
    """JSON-encode a sidecar dict, or ``None`` → ``None`` (stored as NULL)."""
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a JSON sidecar column back to a dict; tolerate bad/blank data."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------- writes
def record_readings(
    rows: List[Reading], ts: Optional[int] = None, path: Optional[Path] = None
) -> int:
    """Persist a batch of readings, all stamped with one ``ts`` (epoch seconds).

    Returns the number of rows written. ``value_num`` stays ``NULL`` for an
    absent numeric reading (asleep ≠ 0). Blocking SQLite — callers on the event
    loop wrap this in :func:`asyncio.to_thread`.
    """
    if not rows:
        return 0
    when = int(ts if ts is not None else time.time())
    payload = [
        (
            when,
            r.domain,
            r.entity_id,
            r.metric,
            r.value_num,
            r.value_txt,
            r.unit,
            r.quality,
            _dumps(r.attrs),
        )
        for r in rows
    ]
    with _connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO readings (
                ts, domain, entity_id, metric, value_num, value_txt,
                unit, quality, attrs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
    return len(payload)


def record_event(
    domain: str,
    event_type: str,
    *,
    entity_id: Optional[str] = None,
    source: Optional[str] = None,
    outcome: Optional[str] = None,
    severity: str = "info",
    payload: Optional[Dict[str, Any]] = None,
    ts: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Persist one discrete event/trigger. ``ts`` defaults to now."""
    when = int(ts if ts is not None else time.time())
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO events (
                ts, domain, entity_id, event_type, source, outcome, severity, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (when, domain, entity_id, event_type, source, outcome, severity, _dumps(payload)),
        )
        conn.commit()


# --------------------------------------------------------------- reads
def _filtered_select(
    table: str,
    filters: Dict[str, Any],
    since: Optional[int],
    until: Optional[int],
    limit: int,
) -> tuple[str, List[Any]]:
    """Build a parametrized ``SELECT … WHERE … ORDER BY ts DESC LIMIT`` query.

    Only non-``None`` filters contribute a clause, so callers pass just the
    facets they care about. Values are bound, never interpolated.
    """
    clauses: List[str] = []
    params: List[Any] = []
    for col, val in filters.items():
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(int(since))
    if until is not None:
        clauses.append("ts < ?")
        params.append(int(until))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, int(limit)))
    return f"SELECT * FROM {table}{where} ORDER BY ts DESC LIMIT ?", params


def read_events(
    *,
    domain: Optional[str] = None,
    event_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    limit: int = 200,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return events matching the (optional) filters, newest first.

    This is what the ``GET /api/activity`` endpoint (issue #289) calls.
    """
    query, params = _filtered_select(
        "events",
        {"domain": domain, "event_type": event_type, "entity_id": entity_id},
        since,
        until,
        limit,
    )
    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "ts": int(r["ts"]),
            "domain": r["domain"],
            "entity_id": r["entity_id"],
            "event_type": r["event_type"],
            "source": r["source"],
            "outcome": r["outcome"],
            "severity": r["severity"],
            "payload": _loads(r["payload"]),
        }
        for r in rows
    ]


def read_readings(
    *,
    domain: Optional[str] = None,
    entity_id: Optional[str] = None,
    metric: Optional[str] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    limit: int = 500,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return readings matching the (optional) filters, newest first.

    ``value_num`` is passed through unchanged — ``None`` (asleep) stays ``None``.
    """
    query, params = _filtered_select(
        "readings",
        {"domain": domain, "entity_id": entity_id, "metric": metric},
        since,
        until,
        limit,
    )
    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "ts": int(r["ts"]),
            "domain": r["domain"],
            "entity_id": r["entity_id"],
            "metric": r["metric"],
            "value_num": r["value_num"],
            "value_txt": r["value_txt"],
            "unit": r["unit"],
            "quality": r["quality"],
            "attrs": _loads(r["attrs"]),
        }
        for r in rows
    ]


# --------------------------------------------------------------- retention
def compact_and_prune(
    config: Optional[TelemetryConfig] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Delete rows past their retention window (prune-only — no rollups yet).

    Raw ``readings`` are pruned on the short window; ``events`` on the much
    longer one. Reading rollups are deliberately deferred (issue #290: start
    raw-only, measure, add rollups only if volume warrants).
    """
    cfg = config or load_telemetry_config()
    current = int(now if now is not None else time.time())
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM readings WHERE ts < ?",
            (current - cfg.readings_retention_seconds,),
        )
        conn.execute(
            "DELETE FROM events WHERE ts < ?",
            (current - cfg.events_retention_seconds,),
        )
        conn.commit()
