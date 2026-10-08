"""Server-side network-device history store (SQLite).

A tiny per-MAC registry behind the Network tab's Phase-4 features (issue #129):
``first_seen`` / ``last_seen`` / ``times_seen``, the user's ``important`` flag,
and the online/offline + new-device derivations the tab and its alerts lean on.

**No background sampler.** Unlike :mod:`src.energy_history`, this is updated on
each ``GET /api/network`` read — the NETGEAR SOAP read is comparatively
expensive and the Network tab polls it only while open. So "online" means *seen
in the latest read*, and "offline" means *a known MAC absent from it*. The first
read ever seeds the registry silently (every device would otherwise look new).

Kept deliberately **separate from the rename store**
(``config/network_display_names.json``): that holds the user's label and is the
verbatim-shared flat ``{mac: name}`` map; this holds the observed history plus
the ``important`` flag, which have a different lifecycle (the label survives even
a device never reappearing; the history is observational and self-prunes). The
``important`` flag lives here rather than the rename JSON precisely so that store
stays a plain string map shared verbatim with the unit/plug/detector renames.

**Internet samples (issue #840).** The same file also keeps a small
``internet_samples`` series — external/gateway latency, loss and any speed-test
result — that feeds the sparklines on the internet-health tile. It follows the
same no-sampler rule: a sample is taken from each ``GET /api/network`` read
(throttled to one a minute, a speed-test result always kept), so the series
fills while the tab is open and from any speed test run in the background.

**Randomised MACs are never recorded.** A modern phone rotates a per-SSID
locally-administered address, so it is not a stable device to track — the caller
(:mod:`app.webapp.routers.network`) filters those out before recording, which
also keeps the new-device alert from firing on every MAC rotation.

UI-free: shared by the network API. Never imports the UI. Mirrors the
connection/retention shape of :mod:`src.energy_history`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, ContextManager, Dict, Iterable, List, Optional, Tuple

from src._mac import normalize_mac
from src._sqlite import connect as _sqlite_connect
from src.runtime_data import runtime_db_path

logger = logging.getLogger("network_history")

# Default DB location: the fleet runtime-data root (``C:\sqlite\home-automation\``
# on Windows), next to the energy-history / telemetry stores — see
# :mod:`src.runtime_data` and project-scaffolding#243.
# ``NETWORK_HISTORY_DB_PATH`` (env) overrides it.
DEFAULT_DB_PATH = runtime_db_path(
    "home-automation", "network_history.sqlite3", env_var="NETWORK_HISTORY_DB_PATH"
)

# A device whose first_seen is within this window is flagged ``is_new`` for the
# row badge (the new-device *alert* uses the precise this-cycle set instead).
_NEW_DEVICE_WINDOW_S = 24 * 3600

# Non-important devices unseen for longer than this are pruned so the registry
# can't grow without bound (guest devices, replaced hardware). Important devices
# are never pruned — losing a user-set flag to a long absence would be wrong.
_PRUNE_AFTER_S = 180 * 24 * 3600

# Internet-sample series (#840): at most one ping-only sample per this many
# seconds (the tab polls every ~15 s), kept this long, and bucketed to at most
# _LATENCY_POINTS means when read so the payload stays small.
_SAMPLE_MIN_GAP_S = 60
_SAMPLE_RETENTION_S = 30 * 24 * 3600
_LATENCY_POINTS = 96
_SPEED_POINTS = 30


# --------------------------------------------------------------- connection
def _connect(path: Optional[Path] = None) -> ContextManager[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (mirrors the energy-history store)."""
    return _sqlite_connect(DEFAULT_DB_PATH, path)


# Columns added after the table shipped. A live DB predates them, so init_db
# tops them up rather than relying on CREATE TABLE (which is a no-op once the
# table exists). Each is nullable with no default, so the ALTER is safe.
_ADDED_COLUMNS = {
    # Last observed band and SSID (issue #513): an offline row otherwise has no
    # connection detail at all, and "known but offline, on 2.4 GHz / TestNet-IoT"
    # is exactly what the grouped device view needs to stay legible.
    "last_conn_type": "TEXT",
    "last_ssid": "TEXT",
}


def init_db(path: Optional[Path] = None) -> None:
    """Create the ``devices`` table if it does not exist (idempotent)."""
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                mac        TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL,
                times_seen INTEGER NOT NULL DEFAULT 1,
                last_ip    TEXT,
                last_name  TEXT,
                important  INTEGER NOT NULL DEFAULT 0,
                -- 1 for devices captured on the very first (cold-start) read, so
                -- the "new" badge doesn't light up the whole inventory on day one.
                seeded     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        have = {str(r["name"]) for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
        for column, decl in _ADDED_COLUMNS.items():
            if column not in have:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {column} {decl}")
                logger.info("🧱 network history: added column %s", column)
        conn.commit()


# --------------------------------------------------------------- reads
def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "first_seen": int(row["first_seen"]),
        "last_seen": int(row["last_seen"]),
        "times_seen": int(row["times_seen"]),
        "last_ip": row["last_ip"],
        "last_name": row["last_name"],
        "last_conn_type": row["last_conn_type"],
        "last_ssid": row["last_ssid"],
        "important": bool(row["important"]),
        "seeded": bool(row["seeded"]),
    }


# --------------------------------------------------------------- writes
def record_and_snapshot(
    seen: Iterable[Dict[str, Any]],
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Upsert the currently-seen devices, returning ``(new_macs, full_snapshot)``.

    ``seen`` is the live, **non-randomised** device list — each item a dict with
    ``mac`` (will be normalised), ``ip``, ``name``, and optionally ``conn_type``
    / ``ssid`` (kept as the last-known band/SSID for offline rows). ``new_macs``
    are the MACs
    first observed *this cycle* (the new-device alert source); it is empty on the
    very first populated read so seeding the registry doesn't alert on everything.
    The returned snapshot is the whole registry (online + offline) after the
    update, so a caller needs only this one round-trip.
    """
    when = int(now if now is not None else time.time())
    init_db(path)
    with _connect(path) as conn:
        was_empty = conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"] == 0
        existing = {str(r["mac"]) for r in conn.execute("SELECT mac FROM devices").fetchall()}

        # On the very first populated read every device is a seed (seeded=1), so
        # the "new" badge stays off; later arrivals insert with seeded=0.
        seeded = 1 if was_empty else 0
        new_macs: List[str] = []
        for item in seen:
            mac = normalize_mac(item.get("mac", ""))
            if not mac:
                continue
            if mac not in existing and not was_empty:
                new_macs.append(mac)
            conn.execute(
                """
                INSERT INTO devices (
                    mac, first_seen, last_seen, times_seen,
                    last_ip, last_name, last_conn_type, last_ssid, seeded
                )
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    last_seen      = excluded.last_seen,
                    times_seen     = devices.times_seen + 1,
                    last_ip        = COALESCE(excluded.last_ip, devices.last_ip),
                    last_name      = COALESCE(excluded.last_name, devices.last_name),
                    last_conn_type = COALESCE(excluded.last_conn_type, devices.last_conn_type),
                    last_ssid      = COALESCE(excluded.last_ssid, devices.last_ssid)
                """,
                (
                    mac,
                    when,
                    when,
                    item.get("ip"),
                    item.get("name"),
                    item.get("conn_type"),
                    item.get("ssid"),
                    seeded,
                ),
            )

        # Bound growth: drop long-absent, non-important devices.
        conn.execute(
            "DELETE FROM devices WHERE important = 0 AND last_seen < ?",
            (when - _PRUNE_AFTER_S,),
        )
        conn.commit()
        rows = conn.execute("SELECT * FROM devices").fetchall()

    snapshot = {str(r["mac"]): _row_to_record(r) for r in rows}
    return new_macs, snapshot


def set_important(
    mac: str,
    important: bool,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Set or clear the ``important`` flag for one device, persisting immediately.

    Upserts so it works even before the device's first recorded read (defensive —
    in practice the detail modal only opens for an already-recorded device).
    """
    key = normalize_mac(mac)
    when = int(now if now is not None else time.time())
    init_db(path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO devices (mac, first_seen, last_seen, times_seen, important)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(mac) DO UPDATE SET important = excluded.important
            """,
            (key, when, when, 1 if important else 0),
        )
        conn.commit()


def is_new(record: Dict[str, Any], now: Optional[int] = None) -> bool:
    """True if the device genuinely appeared recently (not a cold-start seed)."""
    if record.get("seeded"):
        return False
    when = int(now if now is not None else time.time())
    return when - int(record.get("first_seen", 0)) <= _NEW_DEVICE_WINDOW_S


# --------------------------------------------------- internet samples (#840)
def init_internet_samples(path: Optional[Path] = None) -> None:
    """Create the ``internet_samples`` table if it does not exist (idempotent)."""
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internet_samples (
                ts              INTEGER PRIMARY KEY,
                online          INTEGER NOT NULL,
                external_ms     REAL,
                gateway_ms      REAL,
                packet_loss_pct REAL,
                download_mbps   REAL,
                upload_mbps     REAL
            )
            """
        )
        conn.commit()


def record_internet_sample(
    internet: Dict[str, Any],
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> bool:
    """Persist one internet-health reading; returns whether a row was written.

    ``internet`` is the ``internet`` block of the network payload (``online``,
    ``external_ms``, ``gateway_ms``, ``packet_loss_pct``, ``download_mbps``,
    ``upload_mbps``). A reading carrying a speed-test result is always kept; a
    ping-only one is skipped when the previous sample is under
    ``_SAMPLE_MIN_GAP_S`` old, so a 15 s poll doesn't write four rows a minute.
    """
    when = int(now if now is not None else time.time())
    has_speed = (
        internet.get("download_mbps") is not None or internet.get("upload_mbps") is not None
    )
    init_internet_samples(path)
    with _connect(path) as conn:
        if not has_speed:
            last = conn.execute("SELECT MAX(ts) AS ts FROM internet_samples").fetchone()["ts"]
            if last is not None and when - int(last) < _SAMPLE_MIN_GAP_S:
                return False
        conn.execute(
            """
            INSERT OR REPLACE INTO internet_samples (
                ts, online, external_ms, gateway_ms, packet_loss_pct,
                download_mbps, upload_mbps
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when,
                1 if internet.get("online") else 0,
                internet.get("external_ms"),
                internet.get("gateway_ms"),
                internet.get("packet_loss_pct"),
                internet.get("download_mbps"),
                internet.get("upload_mbps"),
            ),
        )
        conn.execute(
            "DELETE FROM internet_samples WHERE ts < ?", (when - _SAMPLE_RETENTION_S,)
        )
        conn.commit()
    return True


def last_speed_sample_ts(path: Optional[Path] = None) -> Optional[int]:
    """Timestamp of the newest stored speed-test result, or ``None`` if there is none.

    The nightly scheduler's "has tonight's test already run?" memory: it lives
    in the store, so a webapp restart can neither lose it nor repeat the test.
    """
    try:
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT MAX(ts) AS ts FROM internet_samples "
                "WHERE download_mbps IS NOT NULL OR upload_mbps IS NOT NULL"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["ts"]) if row and row["ts"] is not None else None


def _bucket_means(
    points: List[Tuple[int, float]], start: int, end: int, buckets: int
) -> List[List[float]]:
    """Mean of ``points`` per equal time bucket across ``[start, end]``, skipping empties."""
    if len(points) <= buckets:
        return [[ts, val] for ts, val in points]
    width = max(1.0, (end - start) / buckets)
    sums: Dict[int, List[float]] = {}
    for ts, val in points:
        idx = min(buckets - 1, max(0, int((ts - start) / width)))
        bucket = sums.setdefault(idx, [0.0, 0.0, 0.0])
        bucket[0] += ts
        bucket[1] += val
        bucket[2] += 1
    return [
        [round(b[0] / b[2]), round(b[1] / b[2], 2)] for _idx, b in sorted(sums.items())
    ]


def internet_history(
    latency_hours: int = 24,
    speed_days: int = 30,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Series for the tile's sparklines: ``latency`` plus ``download`` / ``upload``.

    Each series is ``[[ts, value], ...]`` oldest first. Latency is the external
    round-trip over the last ``latency_hours``, bucket-averaged to at most
    ``_LATENCY_POINTS`` points; the speed series are the last ``_SPEED_POINTS``
    results inside ``speed_days`` (a test is a rare, deliberate event, so they
    are never averaged). A store with no table yet reads as empty.
    """
    when = int(now if now is not None else time.time())
    lat_start = when - latency_hours * 3600
    speed_start = when - speed_days * 86400
    empty: Dict[str, Any] = {
        "latency": [],
        "download": [],
        "upload": [],
        "latency_window_h": latency_hours,
        "speed_window_d": speed_days,
    }
    try:
        with _connect(path) as conn:
            lat_rows = conn.execute(
                "SELECT ts, external_ms FROM internet_samples "
                "WHERE ts >= ? AND external_ms IS NOT NULL ORDER BY ts",
                (lat_start,),
            ).fetchall()
            speed_rows = conn.execute(
                "SELECT ts, download_mbps, upload_mbps FROM internet_samples "
                "WHERE ts >= ? AND (download_mbps IS NOT NULL OR upload_mbps IS NOT NULL) "
                "ORDER BY ts DESC LIMIT ?",
                (speed_start, _SPEED_POINTS),
            ).fetchall()
    except sqlite3.OperationalError:
        # No table yet (nothing recorded on this install) — an empty history.
        return empty
    speed_rows = list(reversed(speed_rows))
    return {
        **empty,
        "latency": _bucket_means(
            [(int(r["ts"]), float(r["external_ms"])) for r in lat_rows],
            lat_start,
            when,
            _LATENCY_POINTS,
        ),
        "download": [
            [int(r["ts"]), float(r["download_mbps"])]
            for r in speed_rows
            if r["download_mbps"] is not None
        ],
        "upload": [
            [int(r["ts"]), float(r["upload_mbps"])]
            for r in speed_rows
            if r["upload_mbps"] is not None
        ],
    }
