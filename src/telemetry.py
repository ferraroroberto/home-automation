"""Unified telemetry store (SQLite) — the standard event + reading substrate.

This module is the recorder/reader core for the home-automation telemetry model
(design: issue #283). It owns a single WAL-mode SQLite file in the fleet
runtime-data root (``<root>/home-automation/telemetry.sqlite3`` — see
:mod:`src.runtime_data`) holding two narrow, append-mostly tables:

* ``readings`` — one row per ``(entity, metric)`` observation. Adding a new
  device type or metric is a new *row*, never a new *column* — so the schema
  never needs an ``ALTER TABLE``. A JSON1 ``attrs`` sidecar carries anything
  rare that doesn't deserve its own column yet.
* ``events`` — one row per discrete event/trigger (arm/disarm, plug toggle,
  power loss, schedule firing, …), with a JSON1 ``payload`` for arbitrary
  detail. This is the unified successor to the scattered write-only
  ``logs/*.jsonl`` trail and the otherwise-ephemeral RISCO event feed.
* ``rollup_hourly`` — one row per completed ``(hour, domain, entity, metric)``,
  folded out of ``readings`` before the short raw window prunes them and kept
  long (default ~400 days). Raw stays the live view; rollups are the history
  (issue #739, discharging the rollup deferral #290 booked on purpose).

**Asleep is not zero.** A metric with no real numeric reading stores
``value_num = NULL`` (never 0), mirroring the convention in
:mod:`src.energy_history` — a gap is a gap, not a misleading 0. Rollups hold
the same line: an hour whose series had no numeric reading at all stores
``avg_num``/``min_num``/``max_num`` as ``NULL``, never 0.

**Missing is not low** (the lesson :mod:`src.energy_history` learned in #579).
An hour that was only *partly* sampled is neither present nor missing: its
average is honest about the minutes that arrived and silent about the ones that
did not. Rollups therefore record ``covered_s`` — how much of the hour the
aggregate actually rests on — and :func:`rollup_coverage` turns that into a
0–1 ratio plus a ``gap`` flag against :data:`MIN_TRUSTED_COVERAGE`, so a dead
sampler is distinguishable from a genuinely idle device.

Coverage is measured in *seconds*, not in sample counts, for the same reason
#579 chose seconds: a count needs a denominator ("how many samples should this
hour have had?"), which is only knowable from the sampler cadence — so a
cadence change would silently re-label every hour of stored history. Seconds
are self-describing. The accrual rule differs from
:mod:`src.energy_history`'s by design, and the difference is not an oversight:
that module *integrates* (the rectangular rule gives each sample the interval
to its right, so an hour's last sample contributes nothing and a healthy hour
lands at 0.917), whereas this one *averages* — here each reading stands for the
time until the next one, capped at :func:`TelemetryConfig.max_gap_seconds`,
including the final stretch to the hour's end, so a healthy hour reaches ~1.0.

**Categorical metrics are decided, not averaged.** ``value_txt`` metrics
(``operation_mode``, ``fan_speed``, ``switch_on``, ``status``) have no mean, so
the rollup records **the hour's last value** (``txt_last``) — the simplest rule
that always yields a real observed state rather than a synthesized one, and the
one that composes correctly when a reader walks hours forward in time. Because
last-value alone would render "heat for 55 minutes, then cool for 5" as a flat
``cool`` hour, it is paired with ``txt_changes``: how many times the value
actually changed inside the hour. That is the categorical counterpart of
``covered_s`` — one integer that stops a settled hour and a thrashing one from
looking identical — and is deliberately *not* a second idiom: change-only rows
were the alternative considered and rejected, because they would break the
uniform one-row-per-``(hour, series)`` shape the numeric side relies on.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Dict, List, Optional

from dotenv import load_dotenv

from src._sqlite import connect as _sqlite_connect
from src.runtime_data import runtime_db_path

logger = logging.getLogger("telemetry")

# Default DB location: the fleet runtime-data root (``C:\sqlite\home-automation\``
# on Windows), not this repo's ``webapp/`` — this store's 20s-interval writes were
# a confirmed contributor to the constant clicking on tower's spinning E: drive
# (project-scaffolding#243). ``TELEMETRY_DB_PATH`` (env) still overrides it, and
# still outranks everything — the e2e subprocess sets it to keep a test boot off
# the real DB.
DEFAULT_DB_PATH = runtime_db_path(
    "home-automation", "telemetry.sqlite3", env_var="TELEMETRY_DB_PATH"
)

_HOUR = 3600

# Fraction of an hour that must actually carry data before the hour's rollup
# counts as a measurement rather than an under-measurement.
#
# Deliberately the same 0.75 as :data:`src.energy_history.MIN_TRUSTED_COVERAGE`:
# one home draws one line between "measured" and "under-measured", and a second
# threshold here would mean the Activity log and the Energy dashboard could
# disagree about whether the same hour was trustworthy. The accrual rules
# differ (see the module docstring) but both leave a healthy hour comfortably
# clear of it, so the shared constant costs nothing and the divergence would.
MIN_TRUSTED_COVERAGE = 0.75

# Set True only once :func:`init_db` runs against the *default* DB (i.e. the live
# webapp, not a test's tmp-path store). The central event mirror in
# :mod:`src.activity_log` checks this so it stays a clean no-op in unit tests
# that never start the webapp, yet activates automatically in production.
_default_db_ready = False


def default_db_ready() -> bool:
    """True once the default telemetry DB has been initialized (webapp running)."""
    return _default_db_ready


@dataclass(frozen=True)
class TelemetryConfig:
    """Retention knobs, loaded from ``.env`` (all optional).

    Raw ``readings`` are kept for a short, bounded window; discrete ``events``
    are far rarer and human-meaningful, so they are kept much longer.
    """

    readings_retention_days: int = 7
    events_retention_days: int = 400
    rollup_retention_days: int = 400
    sample_interval_s: int = 300

    @property
    def readings_retention_seconds(self) -> int:
        return self.readings_retention_days * 24 * _HOUR

    @property
    def events_retention_seconds(self) -> int:
        return self.events_retention_days * 24 * _HOUR

    @property
    def rollup_retention_seconds(self) -> int:
        return self.rollup_retention_days * 24 * _HOUR

    @property
    def max_gap_seconds(self) -> int:
        """How long one reading may stand for before the rest of the gap is a hole.

        Twice the sampler cadence, so a single dropped tick still reads as a
        covered hour while a real outage does not. Derived from the sampler's
        own ``TELEMETRY_SAMPLE_INTERVAL_S`` rather than hardcoded, because a
        fixed cap would silently turn every hour into a phantom outage the day
        the cadence is slowed — the exact failure a count-based denominator
        has, and the reason coverage is measured in seconds at all.
        """
        return 2 * self.sample_interval_s


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
        rollup_retention_days=max(1, _env_int("TELEMETRY_ROLLUP_RETENTION_DAYS", 400)),
        # The sampler's own knob, read here so coverage is judged against the
        # cadence that actually produced the rows (see `max_gap_seconds`).
        sample_interval_s=max(30, _env_int("TELEMETRY_SAMPLE_INTERVAL_S", 300)),
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
def _connect(path: Optional[Path] = None) -> ContextManager[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (concurrent sampler write + API read)."""
    return _sqlite_connect(DEFAULT_DB_PATH, path)


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

            -- Completed-hour rollups of `readings` (#739). WITHOUT ROWID: the
            -- full key *is* the identity, so storing it once in the PK b-tree
            -- rather than again in a separate index roughly halves a table
            -- whose three TEXT key columns outweigh its numeric payload
            -- (measured: ~42 bytes of key per row against the live store).
            --
            -- No secondary index on (domain, entity_id, metric): nothing reads
            -- this table yet, and measured against a real 168-hour fold of the
            -- live store one would cost 54 bytes/row on top of 78 — 31 MB of
            -- the 400-day steady state, to serve no query. A reader that wants
            -- entity-first lookups adds it here as one more `CREATE INDEX IF
            -- NOT EXISTS`; this function is idempotent and runs on every boot,
            -- so it builds itself on the next restart. Measured steady state as
            -- shipped: 78 bytes/row, ~108 KB/day, ~43 MB at 400 days.
            CREATE TABLE IF NOT EXISTS rollup_hourly (
                hour_start  INTEGER NOT NULL,
                domain      TEXT    NOT NULL,
                entity_id   TEXT    NOT NULL,
                metric      TEXT    NOT NULL,
                n           INTEGER NOT NULL,
                num_n       INTEGER NOT NULL,
                avg_num     REAL,
                min_num     REAL,
                max_num     REAL,
                txt_last    TEXT,
                txt_changes INTEGER NOT NULL DEFAULT 0,
                covered_s   REAL,
                unit        TEXT,
                PRIMARY KEY (hour_start, domain, entity_id, metric)
            ) WITHOUT ROWID;
            """
        )
        conn.commit()
    if path is None:
        global _default_db_ready
        _default_db_ready = True


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

    Returns the number of rows written. **A reading with no value at all**
    (both ``value_num`` and ``value_txt`` ``None``) is dropped — an offline or
    non-metering device produces a gap, not a NULL row, so the store isn't
    flooded with meaningless "—" entries. A present numeric ``0`` is kept, and a
    genuinely-missing numeric alongside a text value still records (asleep ≠ 0
    only matters where there is something to record). Blocking SQLite — callers
    on the event loop wrap this in :func:`asyncio.to_thread`.
    """
    rows = [r for r in rows if r.value_num is not None or r.value_txt is not None]
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
def _like_escape(value: str) -> str:
    """Escape SQLite LIKE wildcards so a literal ``%``/``_`` isn't a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filtered_select(
    table: str,
    exact: Dict[str, Any],
    like: Optional[Dict[str, Any]],
    since: Optional[int],
    until: Optional[int],
    limit: int,
) -> tuple[str, List[Any]]:
    """Build a parametrized ``SELECT … WHERE … ORDER BY ts DESC LIMIT`` query.

    ``exact`` columns match with ``=`` (dropdowns: domain, entity); ``like``
    columns match a case-insensitive substring (free-text boxes: event_type,
    metric — so ``w`` finds ``power_w``). Only non-``None`` filters contribute a
    clause. Values are always bound, never interpolated.
    """
    clauses: List[str] = []
    params: List[Any] = []
    for col, val in exact.items():
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    for col, val in (like or {}).items():
        if val is not None:
            clauses.append(f"{col} LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(str(val)) + "%")
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
        {"domain": domain, "entity_id": entity_id},
        {"event_type": event_type},
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
        {"domain": domain, "entity_id": entity_id},
        {"metric": metric},
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


# --------------------------------------------------------------- rollups
def _aggregate_series_hour(
    rows: List[sqlite3.Row], hour_start: int, max_gap: int
) -> Optional[Dict[str, Any]]:
    """Fold one hour of one ``(domain, entity, metric)`` series into a rollup dict.

    ``rows`` are that series' readings inside the hour, ascending by ``ts``.

    Numeric metrics get ``avg_num``/``min_num``/``max_num`` over the readings
    that actually carried a number; a series whose numbers were all ``NULL``
    (asleep, or a purely categorical metric) keeps all three ``None`` rather
    than collapsing to 0. ``covered_s`` is the span the aggregate rests on:
    each reading stands for the time until the next one, capped at ``max_gap``,
    with the final reading standing for the stretch to the hour's end under the
    same cap. Time before the *first* reading is deliberately uncredited — a
    reading speaks for what came after it, not for what it missed.

    Categorical metrics get ``txt_last`` plus ``txt_changes`` (transitions
    between consecutive differing values) — see the module docstring for why
    that pairing, and not change-only rows, is the chosen rule.
    """
    if not rows:
        return None

    hour_end = hour_start + _HOUR
    nums = [float(r["value_num"]) for r in rows if r["value_num"] is not None]
    covered = 0.0
    txt_last: Optional[str] = None
    txt_changes = 0
    unit: Optional[str] = None

    for i, row in enumerate(rows):
        nxt = int(rows[i + 1]["ts"]) if i + 1 < len(rows) else hour_end
        covered += max(0, min(nxt - int(row["ts"]), max_gap))
        if row["unit"] is not None:
            unit = row["unit"]
        value_txt = row["value_txt"]
        if value_txt is not None:
            if txt_last is not None and value_txt != txt_last:
                txt_changes += 1
            txt_last = value_txt

    return {
        "hour_start": hour_start,
        "domain": rows[0]["domain"],
        "entity_id": rows[0]["entity_id"],
        "metric": rows[0]["metric"],
        "n": len(rows),
        "num_n": len(nums),
        "avg_num": round(sum(nums) / len(nums), 4) if nums else None,
        "min_num": min(nums) if nums else None,
        "max_num": max(nums) if nums else None,
        "txt_last": txt_last,
        "txt_changes": txt_changes,
        "covered_s": round(covered, 1),
        "unit": unit,
    }


def rollup_coverage(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{"coverage": 0–1, "gap": bool}`` for one ``rollup_hourly`` row.

    ``coverage`` is the share of the hour the rollup's aggregates rest on;
    ``gap`` is the actionable flag — enough of the hour is missing
    (:data:`MIN_TRUSTED_COVERAGE`) that its average is an under-measurement
    rather than a measurement. This is the one place that line is drawn, so a
    caller never re-derives the threshold.

    Two exclusions :func:`src.energy_history._mark_hourly_coverage` carries do
    *not* apply here, and the difference is deliberate. That module skips hours
    with no data at all and hours whose measured generation is 0, because a
    sleeping inverter flapping between 0 W and asleep would otherwise flag
    every night. Neither has an analogue here: a rollup row exists only because
    readings existed, and a device that genuinely read 0 W for a fully-covered
    hour is a measurement, not a shortfall. Coverage alone is the test.

    Only completed hours are ever written (see :func:`compact_and_prune`), so
    the in-progress hour's elapsed-window special case has no home here. A
    future reader that integrates the current hour fresh from raw readings
    would need it — that is where it belongs, not in the stored row.
    """
    covered = float(row.get("covered_s") or 0.0)
    coverage = min(1.0, covered / _HOUR)
    return {"coverage": round(coverage, 3), "gap": coverage < MIN_TRUSTED_COVERAGE}


# --------------------------------------------------------------- retention
def compact_and_prune(
    config: Optional[TelemetryConfig] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Fold completed hours of ``readings`` into ``rollup_hourly``, then prune.

    Ordering is the whole point (issue #739): every completed hour is
    aggregated **before** any raw row is deleted, so the short raw window stops
    being the limit of what the house remembers. Raw ``readings`` are then
    pruned on their short window, ``events`` on their long one, and the
    rollups on their own (default ~400 days, matching ``events``).

    Only hours strictly before the current one are rolled up — the in-progress
    hour is still filling, and a rollup written from a third of it would be
    indistinguishable from a real outage. Work resumes from the newest hour
    already rolled up (or the oldest surviving raw row on first run, which is
    what backfills existing history), mirroring
    :func:`src.energy_history.compact_and_prune`.
    """
    cfg = config or load_telemetry_config()
    current = int(now if now is not None else time.time())
    current_hour = current - (current % _HOUR)
    rolled = 0

    with _connect(path) as conn:
        last_rolled = conn.execute(
            "SELECT MAX(hour_start) AS h FROM rollup_hourly"
        ).fetchone()["h"]
        oldest = conn.execute("SELECT MIN(ts) AS t FROM readings").fetchone()["t"]
        if oldest is None:
            start_hour = current_hour
        elif last_rolled is None:
            start_hour = int(oldest) - (int(oldest) % _HOUR)
        else:
            start_hour = int(last_rolled) + _HOUR

        hour = start_hour
        while hour < current_hour:
            rows = conn.execute(
                """
                SELECT * FROM readings WHERE ts >= ? AND ts < ?
                ORDER BY domain, entity_id, metric, ts
                """,
                (hour, hour + _HOUR),
            ).fetchall()
            series: Dict[tuple, List[sqlite3.Row]] = {}
            for row in rows:
                series.setdefault(
                    (row["domain"], row["entity_id"], row["metric"]), []
                ).append(row)
            for group in series.values():
                roll = _aggregate_series_hour(group, hour, cfg.max_gap_seconds)
                if roll is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rollup_hourly (
                        hour_start, domain, entity_id, metric, n, num_n,
                        avg_num, min_num, max_num, txt_last, txt_changes,
                        covered_s, unit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        roll["hour_start"], roll["domain"], roll["entity_id"],
                        roll["metric"], roll["n"], roll["num_n"], roll["avg_num"],
                        roll["min_num"], roll["max_num"], roll["txt_last"],
                        roll["txt_changes"], roll["covered_s"], roll["unit"],
                    ),
                )
                rolled += 1
            hour += _HOUR

        conn.execute(
            "DELETE FROM readings WHERE ts < ?",
            (current - cfg.readings_retention_seconds,),
        )
        conn.execute(
            "DELETE FROM events WHERE ts < ?",
            (current - cfg.events_retention_seconds,),
        )
        conn.execute(
            "DELETE FROM rollup_hourly WHERE hour_start < ?",
            (current - cfg.rollup_retention_seconds,),
        )
        conn.commit()

    if rolled:
        logger.info("🧮 Compacted %d series-hour(s) into rollup_hourly", rolled)
