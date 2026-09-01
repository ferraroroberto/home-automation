r"""Wi-Fi walk-test (site-survey) sample store — SQLite (issue #547).

Backs the Network tab's **Walk test** card: you stand somewhere with the phone,
tap *Record here*, and one sample lands in this table — what the AP/router
measured for that client at that moment, plus what the browser measured of its
own round-trip to the server.

**Why the sample comes from the infrastructure, not the phone.** No browser on
any platform exposes Wi-Fi telemetry: there is no ``navigator.wifi``, and
``navigator.connection`` is unimplemented in WebKit, so an iPhone PWA cannot read
SSID, RSSI, or scan for radios — and neither can a native iOS app without the
``NEHotspotHelper`` entitlement Apple reserves for hotspot vendors. So the phone
is the *probe* and the AP/router is the *meter*: the server asks
:func:`src.network_client.resolve_wireless_client_by_mac` how well it currently
hears that MAC. The full reasoning lives in the README's Walk-test section.

**Three ways a sample can lack a signal, and they are not interchangeable.** A
MAC both boxes agree is off the air stores as ``source='not_found'`` — the UI
says *not seen on either radio*, which is the strongest result a walk test can
produce. A MAC nobody could be asked about, because the AP read timed out or the
router login failed, stores as ``source='unknown'`` instead: that is an outage,
not a dead zone, and rendering it as one would invent a coverage claim out of a
failed probe (the repo's "a check that can't establish a fact reports its own
state" rule). Only a genuine reading gets ``found=True`` and a bar.

Kept separate from :mod:`src.network_history` — that is a per-MAC registry
updated on every ``GET /api/network`` poll, this is an append-only log of
deliberate, user-initiated measurements with its own retention. UI-free: shared
by the network API, never imports the UI. Mirrors the connection/retention shape
of :mod:`src.network_history` and :mod:`src.energy_history`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, ContextManager, Dict, List, Optional

from src._mac import normalize_mac
from src._sqlite import connect as _sqlite_connect
from src.runtime_data import runtime_db_path

logger = logging.getLogger("network_survey")

# Default DB location: the fleet runtime-data root (``C:\sqlite\home-automation\``
# on Windows), next to the energy / network history DBs — see
# :mod:`src.runtime_data` and project-scaffolding#243.
# ``NETWORK_SURVEY_DB_PATH`` (env) overrides it.
DEFAULT_DB_PATH = runtime_db_path(
    "home-automation", "network_survey.sqlite3", env_var="NETWORK_SURVEY_DB_PATH"
)

# Samples older than this are dropped on write. A walk test is about the house as
# it is *now*; a reading from two APs and a router firmware ago is misleading, not
# historical. Generous enough to compare across a year of seasons/furniture.
_PRUNE_AFTER_S = 365 * 24 * 3600

# Both boxes answered and neither had the MAC associated — a real dead zone.
SOURCE_NOT_FOUND = "not_found"
# At least one box could not be read, so its radios can't be ruled out — the
# sample establishes nothing about coverage. See the module docstring.
SOURCE_UNKNOWN = "unknown"
# The two ways a sample carries no measurement. Neither counts as `found`, but
# only the first is a statement about coverage.
_UNMEASURED_SOURCES = (SOURCE_NOT_FOUND, SOURCE_UNKNOWN)


def _norm_room(room: str) -> str:
    """Collapse a free-text room label so 'Kitchen ' and 'kitchen' are one room."""
    return " ".join((room or "").split())


# --------------------------------------------------------------- connection
def _connect(path: Optional[Path] = None) -> ContextManager[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (mirrors the network/energy history stores)."""
    return _sqlite_connect(DEFAULT_DB_PATH, path)


def init_db(path: Optional[Path] = None) -> None:
    """Create the ``samples`` table + room index if absent (idempotent)."""
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at INTEGER NOT NULL,
                room        TEXT NOT NULL,
                mac         TEXT NOT NULL,
                -- Measured by the AP/router for this client. NULL signal +
                -- source='not_found' is the "on neither radio" state.
                signal      INTEGER,
                link_rate   INTEGER,
                band        TEXT,
                ssid        TEXT,
                source      TEXT,
                -- Measured by the browser against this server, same instant.
                rtt_ms          REAL,
                jitter_ms       REAL,
                loss_pct        REAL,
                throughput_mbps REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_room ON samples(room)")
        conn.commit()


# --------------------------------------------------------------- reads
def _row_to_sample(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "recorded_at": int(row["recorded_at"]),
        "room": row["room"],
        "mac": row["mac"],
        "signal": None if row["signal"] is None else int(row["signal"]),
        "link_rate": None if row["link_rate"] is None else int(row["link_rate"]),
        "band": row["band"],
        "ssid": row["ssid"],
        "source": row["source"],
        "found": row["source"] not in _UNMEASURED_SOURCES,
        "rtt_ms": row["rtt_ms"],
        "jitter_ms": row["jitter_ms"],
        "loss_pct": row["loss_pct"],
        "throughput_mbps": row["throughput_mbps"],
    }


def load_samples(limit: int = 500, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return the most recent samples, newest first (empty if the DB is fresh)."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM samples ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [_row_to_sample(r) for r in rows]


def room_summary(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One row per room: the latest reading plus the best/worst signal ever seen.

    Sorted weakest-first on the latest signal, because the whole point of a walk
    test is to surface the rooms that need attention. Rooms whose latest sample
    found the device on neither radio sort first of all — an unreachable spot is
    the most extreme coverage result, not a missing one. Rooms whose latest
    sample established nothing (``source='unknown'``) sort **last**: they are not
    a bad result, they are the absence of one, and ranking them alongside real
    dead zones would put an AP outage at the top of a coverage report.

    Aggregated in Python rather than SQL: the sample count is small and the
    "latest row per group plus two extremes" shape reads far more clearly here
    than as a window query.
    """
    samples = load_samples(limit=10_000, path=path)
    rooms: Dict[str, Dict[str, Any]] = {}
    for s in samples:
        # load_samples is newest-first, so the first sighting of a room is its latest.
        entry = rooms.get(s["room"])
        if entry is None:
            entry = {
                "room": s["room"],
                "count": 0,
                "last_recorded_at": s["recorded_at"],
                "last_signal": s["signal"],
                "last_band": s["band"],
                "last_ssid": s["ssid"],
                "last_source": s["source"],
                "last_found": s["found"],
                "last_link_rate": s["link_rate"],
                "last_rtt_ms": s["rtt_ms"],
                "last_throughput_mbps": s["throughput_mbps"],
                "best_signal": None,
                "worst_signal": None,
            }
            rooms[s["room"]] = entry
        entry["count"] += 1
        if s["signal"] is not None:
            best, worst = entry["best_signal"], entry["worst_signal"]
            entry["best_signal"] = s["signal"] if best is None else max(best, s["signal"])
            entry["worst_signal"] = s["signal"] if worst is None else min(worst, s["signal"])

    def _rank(entry: Dict[str, Any]) -> tuple:
        # Dead zones first, then ascending signal, then the rooms that measured
        # nothing at all; room name breaks ties so the order is stable.
        if entry["last_source"] == SOURCE_UNKNOWN:
            band = 2
        elif entry["last_source"] == SOURCE_NOT_FOUND:
            band = 0
        else:
            band = 1
        signal = entry["last_signal"]
        return (band, signal if signal is not None else 0, entry["room"])

    return sorted(rooms.values(), key=_rank)


def known_rooms(path: Optional[Path] = None) -> List[str]:
    """Every room label ever used, alphabetically — backs the room-input datalist."""
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT DISTINCT room FROM samples ORDER BY room").fetchall()
    return [str(r["room"]) for r in rows]


# --------------------------------------------------------------- writes
def record_sample(
    room: str,
    mac: str,
    signal: Optional[int] = None,
    link_rate: Optional[int] = None,
    band: Optional[str] = None,
    ssid: Optional[str] = None,
    # Defaults to "we established nothing", not "no coverage": a caller that
    # supplies no source has not proved the client is off the air.
    source: str = SOURCE_UNKNOWN,
    rtt_ms: Optional[float] = None,
    jitter_ms: Optional[float] = None,
    loss_pct: Optional[float] = None,
    throughput_mbps: Optional[float] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one survey sample and return it as stored.

    Raises :class:`ValueError` on an empty room or MAC — a sample with no place
    or no subject cannot be compared against anything, so it is a caller bug
    rather than something to store and render as a mystery row.
    """
    room_key = _norm_room(room)
    mac_key = normalize_mac(mac)
    if not room_key:
        raise ValueError("room is required")
    if not mac_key:
        raise ValueError("mac is required")

    when = int(now if now is not None else time.time())
    init_db(path)
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO samples (
                recorded_at, room, mac, signal, link_rate, band, ssid, source,
                rtt_ms, jitter_ms, loss_pct, throughput_mbps
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when, room_key, mac_key, signal, link_rate, band, ssid, source,
                rtt_ms, jitter_ms, loss_pct, throughput_mbps,
            ),
        )
        sample_id = int(cur.lastrowid)
        dropped = conn.execute(
            "DELETE FROM samples WHERE recorded_at < ?", (when - _PRUNE_AFTER_S,)
        ).rowcount
        conn.commit()
        row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if dropped:
        logger.info("🧹 network survey: pruned %d sample(s) past retention", dropped)
    logger.info(
        "ℹ️ survey sample recorded: room=%s signal=%s source=%s", room_key, signal, source
    )
    return _row_to_sample(row)


def delete_sample(sample_id: int, path: Optional[Path] = None) -> bool:
    """Delete one sample by id; True if a row was actually removed."""
    init_db(path)
    with _connect(path) as conn:
        deleted = conn.execute("DELETE FROM samples WHERE id = ?", (int(sample_id),)).rowcount
        conn.commit()
    return bool(deleted)


def delete_room(room: str, path: Optional[Path] = None) -> int:
    """Delete every sample for one room; returns how many were removed."""
    room_key = _norm_room(room)
    if not room_key:
        return 0
    init_db(path)
    with _connect(path) as conn:
        deleted = conn.execute("DELETE FROM samples WHERE room = ?", (room_key,)).rowcount
        conn.commit()
    if deleted:
        logger.info("🧹 network survey: deleted %d sample(s) for room %s", deleted, room_key)
    return int(deleted)
