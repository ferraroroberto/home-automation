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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("network_history")

# Default DB location: the repo's gitignored runtime area, next to the energy
# history DB / logs / certs (covered by ``webapp/*.sqlite3`` in .gitignore).
DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent / "webapp" / "network_history.sqlite3"
)

# A device whose first_seen is within this window is flagged ``is_new`` for the
# row badge (the new-device *alert* uses the precise this-cycle set instead).
_NEW_DEVICE_WINDOW_S = 24 * 3600

# Non-important devices unseen for longer than this are pruned so the registry
# can't grow without bound (guest devices, replaced hardware). Important devices
# are never pruned — losing a user-set flag to a long absence would be wrong.
_PRUNE_AFTER_S = 180 * 24 * 3600


def _norm(mac: str) -> str:
    """Canonical key form: upper-case, whitespace-trimmed (matches the rename store)."""
    return (mac or "").strip().upper()


# --------------------------------------------------------------- connection
@contextmanager
def _connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (mirrors the energy-history store)."""
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
        conn.commit()


# --------------------------------------------------------------- reads
def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "first_seen": int(row["first_seen"]),
        "last_seen": int(row["last_seen"]),
        "times_seen": int(row["times_seen"]),
        "last_ip": row["last_ip"],
        "last_name": row["last_name"],
        "important": bool(row["important"]),
        "seeded": bool(row["seeded"]),
    }


def load_network_history(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Return ``{mac: record}`` for every known device (empty if the DB is fresh)."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM devices").fetchall()
    return {str(r["mac"]): _row_to_record(r) for r in rows}


# --------------------------------------------------------------- writes
def record_and_snapshot(
    seen: Iterable[Dict[str, Any]],
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Upsert the currently-seen devices, returning ``(new_macs, full_snapshot)``.

    ``seen`` is the live, **non-randomised** device list — each item a dict with
    ``mac`` (will be normalised), ``ip``, ``name``. ``new_macs`` are the MACs
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
            mac = _norm(item.get("mac", ""))
            if not mac:
                continue
            if mac not in existing and not was_empty:
                new_macs.append(mac)
            conn.execute(
                """
                INSERT INTO devices (mac, first_seen, last_seen, times_seen, last_ip, last_name, seeded)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(mac) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    times_seen = devices.times_seen + 1,
                    last_ip    = COALESCE(excluded.last_ip, devices.last_ip),
                    last_name  = COALESCE(excluded.last_name, devices.last_name)
                """,
                (mac, when, when, item.get("ip"), item.get("name"), seeded),
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
    key = _norm(mac)
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
