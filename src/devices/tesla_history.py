"""Local Tesla history store (SQLite): samples, drives and charging sessions.

Lives in the fleet runtime-data folder (``tesla_history.sqlite3``;
``TESLA_HISTORY_DB_PATH`` overrides it) through the shared WAL helper.

* ``samples`` — every read of an awake car, kept for a bounded window
  (``TESLA_RAW_RETENTION_DAYS``, default 90).
* ``drives`` / ``charging_sessions`` — built from the samples by
  :func:`build_sessions` and kept indefinitely (a few rows a day). The weekly
  and monthly figures are grouped from these on read, so there is no rollup
  table to keep in sync.
* ``status`` — one row: the last connectivity state seen and when, so any
  process (not just the sampler's) can show "asleep" and "last seen".

**No location.** A sample carries only an ``at_home`` flag; coordinates are
dropped in :mod:`src.devices.tesla_client` before anything reaches here.

Drive energy is estimated from the battery-% drop times the usable capacity
(``TESLA_USABLE_KWH``, default 60 for a Model Y RWD) — an estimate, not a
meter. Charging energy is the car's own ``charge_energy_added``.

UI-free: shared by the sampler and the API.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ContextManager, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from src._env import _env_float, _env_int
from src._sqlite import connect as _sqlite_connect
from src.devices.tesla_client import TeslaSample
from src.local_time import HOME_TZ
from src.runtime_data import runtime_db_path

DEFAULT_DB_PATH = runtime_db_path(
    "home-automation", "tesla_history.sqlite3", env_var="TESLA_HISTORY_DB_PATH"
)

# A silence longer than this closes a charging run at its last charging read.
# Generous on purpose: while charging the car is read every 2 min, but a Tesla
# outage backs off up to 30 min and a tray restart adds more, and splitting one
# overnight charge in two is worse than a late close. A real new session is
# caught sooner by ``charge_energy_added`` restarting from a lower value.
_SESSION_GAP_S = 3 * 3600
# Odometer growth below this between two parked reads is GPS/rounding noise,
# not a drive the poll cadence missed.
_MIN_DRIVE_KM = 0.2


@dataclass(frozen=True)
class TeslaHistoryConfig:
    """Store knobs, from ``.env`` (all optional)."""

    usable_kwh: float = 60.0
    raw_retention_days: int = 90


def load_history_config() -> TeslaHistoryConfig:
    load_dotenv(override=True)
    return TeslaHistoryConfig(
        usable_kwh=max(1.0, _env_float("TESLA_USABLE_KWH", 60.0)),
        raw_retention_days=max(1, _env_int("TESLA_RAW_RETENTION_DAYS", 90)),
    )


@dataclass(frozen=True)
class Drive:
    start_ts: int
    end_ts: int
    km: float
    start_pct: Optional[float]
    end_pct: Optional[float]
    energy_kwh: Optional[float]


@dataclass(frozen=True)
class ChargingSession:
    start_ts: int
    end_ts: int
    kwh_added: Optional[float]
    peak_kw: Optional[float]
    start_pct: Optional[float]
    end_pct: Optional[float]
    location: str  # home | away | unknown
    source: str = "car"


# --------------------------------------------------------------- connection
def _connect(path: Optional[Path] = None) -> ContextManager[sqlite3.Connection]:
    return _sqlite_connect(DEFAULT_DB_PATH, path)


def init_db(path: Optional[Path] = None) -> None:
    """Create the tables if they do not exist (idempotent)."""
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER PRIMARY KEY,
                battery_pct REAL,
                range_km REAL,
                charging_state TEXT,
                charger_power_kw REAL,
                charge_energy_added_kwh REAL,
                odometer_km REAL,
                shift_state TEXT,
                speed_kmh REAL,
                at_home INTEGER
            );
            CREATE TABLE IF NOT EXISTS drives (
                start_ts INTEGER PRIMARY KEY,
                end_ts INTEGER NOT NULL,
                km REAL NOT NULL,
                start_pct REAL,
                end_pct REAL,
                energy_kwh REAL
            );
            CREATE TABLE IF NOT EXISTS charging_sessions (
                start_ts INTEGER PRIMARY KEY,
                end_ts INTEGER NOT NULL,
                kwh_added REAL,
                peak_kw REAL,
                start_pct REAL,
                end_pct REAL,
                location TEXT NOT NULL CHECK (location IN ('home', 'away', 'unknown')),
                source TEXT NOT NULL DEFAULT 'car'
            );
            CREATE TABLE IF NOT EXISTS status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL,
                checked_at INTEGER NOT NULL,
                error TEXT
            );
            """
        )


# ------------------------------------------------------------------ writes
def record_sample(sample: TeslaSample, path: Optional[Path] = None) -> None:
    row = sample.to_dict()
    row["at_home"] = None if sample.at_home is None else int(sample.at_home)
    with _connect(path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO samples
               (ts, battery_pct, range_km, charging_state, charger_power_kw,
                charge_energy_added_kwh, odometer_km, shift_state, speed_kmh, at_home)
               VALUES (:ts, :battery_pct, :range_km, :charging_state, :charger_power_kw,
                       :charge_energy_added_kwh, :odometer_km, :shift_state, :speed_kmh, :at_home)""",
            row,
        )
        conn.commit()


def record_status(state: str, error: Optional[str] = None, *, ts: Optional[int] = None,
                  path: Optional[Path] = None) -> None:
    with _connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO status (id, state, checked_at, error) VALUES (1, ?, ?, ?)",
            (state, int(ts if ts is not None else time.time()), error),
        )
        conn.commit()


# -------------------------------------------------------- session building
def _row_to_sample(row: sqlite3.Row) -> TeslaSample:
    at_home = row["at_home"]
    return TeslaSample(
        ts=row["ts"],
        battery_pct=row["battery_pct"],
        range_km=row["range_km"],
        charging_state=row["charging_state"],
        charger_power_kw=row["charger_power_kw"],
        charge_energy_added_kwh=row["charge_energy_added_kwh"],
        odometer_km=row["odometer_km"],
        shift_state=row["shift_state"],
        speed_kmh=row["speed_kmh"],
        at_home=None if at_home is None else bool(at_home),
    )


def _drive_between(start: TeslaSample, end: TeslaSample, start_ts: int,
                   usable_kwh: float) -> Optional[Drive]:
    if start.odometer_km is None or end.odometer_km is None:
        return None
    km = round(end.odometer_km - start.odometer_km, 2)
    if km < _MIN_DRIVE_KM:
        return None
    energy = None
    if start.battery_pct is not None and end.battery_pct is not None:
        energy = round(max(0.0, start.battery_pct - end.battery_pct) / 100.0 * usable_kwh, 2)
    return Drive(start_ts=start_ts, end_ts=end.ts, km=km, start_pct=start.battery_pct,
                 end_pct=end.battery_pct, energy_kwh=energy)


def _build_drives(samples: List[TeslaSample], usable_kwh: float) -> List[Drive]:
    """Drives between consecutive parked reads.

    A drive is whatever moved the odometer between two parked samples: a run
    of driving samples in between, or none at all when the poll cadence missed
    a short trip. It starts at the first driving sample (or the earlier parked
    read when none was seen) and is complete only once a parked read closes it.
    """
    drives: List[Drive] = []
    anchor: Optional[TeslaSample] = None
    first_driving: Optional[TeslaSample] = None
    for sample in samples:
        if sample.driving:
            if first_driving is None:
                first_driving = sample
            continue
        if anchor is not None:
            start_ts = first_driving.ts if first_driving is not None else anchor.ts
            drive = _drive_between(anchor, sample, start_ts, usable_kwh)
            if drive is not None:
                drives.append(drive)
        anchor, first_driving = sample, None
    return drives


def _session_from_run(run: List[TeslaSample], closer: TeslaSample) -> ChargingSession:
    energies = [s.charge_energy_added_kwh for s in (*run, closer) if s.charge_energy_added_kwh is not None]
    powers = [s.charger_power_kw for s in run if s.charger_power_kw is not None]
    homes = {s.at_home for s in run if s.at_home is not None}
    location = "home" if True in homes else ("away" if homes == {False} else "unknown")
    return ChargingSession(
        start_ts=run[0].ts,
        end_ts=closer.ts,
        kwh_added=round(max(energies), 2) if energies else None,
        peak_kw=max(powers) if powers else None,
        start_pct=run[0].battery_pct,
        end_pct=closer.battery_pct,
        location=location,
    )


def _energy_restarted(prev: TeslaSample, cur: TeslaSample) -> bool:
    """True when the per-session energy counter went backwards: a new session began."""
    if not (prev.charging and cur.charging):
        return False
    if prev.charge_energy_added_kwh is None or cur.charge_energy_added_kwh is None:
        return False
    return cur.charge_energy_added_kwh < prev.charge_energy_added_kwh


def _build_charging(samples: List[TeslaSample]) -> List[ChargingSession]:
    """Charging sessions: runs of charging samples, closed by the next non-charging read.

    ``charge_energy_added`` is cumulative within a session, so the session's
    energy is its largest value, the closing read (``Complete``/``Stopped``)
    included because it carries the final figure. A silence longer than
    :data:`_SESSION_GAP_S`, or the counter restarting lower, closes a run at
    its last charging read.
    """
    sessions: List[ChargingSession] = []
    run: List[TeslaSample] = []
    for sample in samples:
        if run and (sample.ts - run[-1].ts > _SESSION_GAP_S or _energy_restarted(run[-1], sample)):
            sessions.append(_session_from_run(run, run[-1]))
            run = []
        if sample.charging:
            run.append(sample)
        elif run:
            sessions.append(_session_from_run(run, sample))
            run = []
    return sessions


def build_sessions(samples: Iterable[TeslaSample], usable_kwh: float) -> Tuple[List[Drive], List[ChargingSession]]:
    """Completed drives and charging sessions from time-ordered samples (pure)."""
    ordered = sorted(samples, key=lambda s: s.ts)
    return _build_drives(ordered, usable_kwh), _build_charging(ordered)


def refresh_sessions(config: TeslaHistoryConfig, path: Optional[Path] = None) -> None:
    """Store the sessions completed since the last stored one (idempotent).

    Each kind is scanned from the end of its latest stored row, so a pruned
    stretch is never rescanned into a truncated duplicate. A drive is anchored
    by the parked read that closed it, so its scan includes that read. A
    charging session may instead be closed at its own last charging read (a
    silence or a counter restart), so its scan starts strictly after the end:
    re-reading that sample would store it again as a one-sample session.
    """
    with _connect(path) as conn:
        def samples_since(table: str, *, inclusive: bool) -> List[TeslaSample]:
            since = conn.execute(f"SELECT COALESCE(MAX(end_ts), 0) FROM {table}").fetchone()[0]  # noqa: S608 — fixed table names
            bound = ">=" if inclusive else ">"
            rows = conn.execute(f"SELECT * FROM samples WHERE ts {bound} ? ORDER BY ts", (since,)).fetchall()  # noqa: S608 — fixed operator
            return [_row_to_sample(r) for r in rows]

        drives, _ = build_sessions(samples_since("drives", inclusive=True), config.usable_kwh)
        _, sessions = build_sessions(samples_since("charging_sessions", inclusive=False), config.usable_kwh)
        conn.executemany(
            "INSERT OR REPLACE INTO drives VALUES (:start_ts, :end_ts, :km, :start_pct, :end_pct, :energy_kwh)",
            [asdict(d) for d in drives],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO charging_sessions VALUES"
            " (:start_ts, :end_ts, :kwh_added, :peak_kw, :start_pct, :end_pct, :location, :source)",
            [asdict(s) for s in sessions],
        )
        conn.commit()


def prune(config: TeslaHistoryConfig, path: Optional[Path] = None) -> int:
    """Drop raw samples past retention; drives and sessions are kept."""
    cutoff = int(time.time()) - config.raw_retention_days * 86400
    with _connect(path) as conn:
        deleted = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        conn.commit()
    return deleted


# ------------------------------------------------------------------- reads
def latest(path: Optional[Path] = None) -> Dict[str, Any]:
    """The last stored sample and the last connectivity state seen."""
    with _connect(path) as conn:
        sample = conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        status = conn.execute("SELECT state, checked_at, error FROM status WHERE id = 1").fetchone()
    latest_sample = _row_to_sample(sample) if sample else None
    return {
        "sample": {**latest_sample.to_dict(), "charging": latest_sample.charging} if latest_sample else None,
        "state": status["state"] if status else None,
        "checked_at": status["checked_at"] if status else None,
        "error": status["error"] if status else None,
    }


def recent_sessions(limit: int = 20, path: Optional[Path] = None) -> Dict[str, List[Dict[str, Any]]]:
    with _connect(path) as conn:
        drives = conn.execute("SELECT * FROM drives ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
        charges = conn.execute(
            "SELECT * FROM charging_sessions ORDER BY start_ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"drives": [dict(r) for r in drives], "charging": [dict(r) for r in charges]}


def _bucket_start(ts: int, period: str) -> datetime:
    local = datetime.fromtimestamp(ts, HOME_TZ)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def history(period: str, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Per-week or per-month km, drive energy and charged energy (oldest first).

    Buckets are Europe/Madrid calendar weeks (Monday start) or months, keyed by
    each drive's or session's start. ``kwh_per_100km`` is ``None`` for a bucket
    with no distance or no energy estimate.
    """
    if period not in {"week", "month"}:
        raise ValueError("period must be 'week' or 'month'")
    with _connect(path) as conn:
        drives = conn.execute("SELECT start_ts, km, energy_kwh FROM drives").fetchall()
        charges = conn.execute("SELECT start_ts, kwh_added, location FROM charging_sessions").fetchall()
    buckets: Dict[datetime, Dict[str, Any]] = {}

    def bucket(ts: int) -> Dict[str, Any]:
        key = _bucket_start(ts, period)
        return buckets.setdefault(key, {
            "start": key.isoformat(), "km": 0.0, "drive_kwh": 0.0, "drives": 0,
            "charged_kwh": 0.0, "charged_home_kwh": 0.0, "charging_sessions": 0,
        })

    for row in drives:
        b = bucket(row["start_ts"])
        b["km"] += row["km"] or 0.0
        b["drive_kwh"] += row["energy_kwh"] or 0.0
        b["drives"] += 1
    for row in charges:
        b = bucket(row["start_ts"])
        b["charged_kwh"] += row["kwh_added"] or 0.0
        if row["location"] == "home":
            b["charged_home_kwh"] += row["kwh_added"] or 0.0
        b["charging_sessions"] += 1
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        for field in ("km", "drive_kwh", "charged_kwh", "charged_home_kwh"):
            b[field] = round(b[field], 2)
        b["kwh_per_100km"] = round(b["drive_kwh"] / b["km"] * 100, 1) if b["km"] and b["drive_kwh"] else None
        out.append(b)
    return out
