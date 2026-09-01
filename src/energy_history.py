"""Server-side energy-history store (SQLite).

The webapp samples the live FusionSolar energy flow on a slow cadence and persists
each reading here so the Energy dashboard can draw recent, hourly, daily, and
monthly history — the read side of the eventual solar load-balancing work.

Two layers, both in one SQLite file under the gitignored runtime area
(``webapp/energy_history.sqlite3``):

* ``samples`` — raw snapshots at the persist cadence (default 60 s), kept for a
  bounded window (default 7 days). Feeds the live "flowing" chart.
* ``rollup_hourly`` — completed-hour energy + average-power rollups, integrated
  from the raw samples and kept long (default ~400 days, still tiny). Daily and
  monthly views are grouped from these on read, so there is no daily/monthly
  table to keep in sync.

**Asleep is not zero.** A sleeping inverter stores ``pv_power_w = NULL`` (never
0). Rollups track how many samples actually had PV, so a bucket with no PV data
is reported ``pv_missing = true`` rather than a misleading 0 Wh.

**Missing is not low** (issue #579). ``pv_missing`` is all-or-nothing, and an
hour that only *partly* had data is neither: its Wh is an integral over the
minutes that arrived, so the minutes that did not contribute nothing rather
than their true energy. Rollups therefore also record ``pv_seconds`` — how much
of the hour the integral actually rests on — and the hour-resolution readers
turn that into ``pv_coverage`` (0–1) plus a ``pv_gap`` flag, so a dead upstream
feed can be told apart from a genuinely dim hour instead of both rendering as a
production collapse.

Energy is integrated from the raw samples (rectangular rule, per-interval gaps
capped at :data:`_MAX_GAP_SECONDS` so a long night-time gap can't inflate a
bucket). It is a household-monitoring estimate, not a billing-grade meter read.

UI-free: shared by the webapp sampler and the energy API. Never imports the UI.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ContextManager, Dict, List, Optional

from dotenv import load_dotenv

from src._sqlite import connect as _sqlite_connect
from src.runtime_data import runtime_db_path
from src.huawei_client import EnergyState

logger = logging.getLogger("energy_history")

# Default DB location: the fleet runtime-data root (``C:\sqlite\home-automation\``
# on Windows), not this repo's ``webapp/`` — see :mod:`src.runtime_data` and
# project-scaffolding#243. ``ENERGY_HISTORY_DB_PATH`` (env) overrides it, matching
# the shape :mod:`src.telemetry` has always had; the three sibling stores here
# lacked an override entirely until then.
DEFAULT_DB_PATH = runtime_db_path(
    "home-automation", "energy_history.sqlite3", env_var="ENERGY_HISTORY_DB_PATH"
)

# Any gap between consecutive samples longer than this is clamped during energy
# integration (the inverter slept, the tray was off, etc.) so a multi-hour gap
# does not integrate one stale power reading across the whole window.
_MAX_GAP_SECONDS = 300

_HOUR = 3600

# Fraction of an hour that must actually carry PV data before the hour's
# integral counts as a measurement rather than an under-measurement (#579).
#
# Not 1.0, because a fully-healthy hour never reaches it: the rectangular rule
# accrues time on the interval *after* each sample, so the hour's last sample
# contributes none — a 60 s live cadence lands at 3540/3600 = 0.983, and an
# hour reconstructed from the cloud's 5-minute series at 3300/3600 = 0.917.
# 0.75 clears both with room for a couple of dropped cloud buckets, while still
# catching the reported outage hours (0.25 and 0.50 coverage).
#
# Public, because the sun-position diagnostic (#590) has to draw the same line
# between a measurement and an under-measurement — an under-covered hour there
# reads as *shading*, which is exactly the conclusion it exists to support. One
# threshold, one home; never re-derived at a call site.
MIN_TRUSTED_COVERAGE = 0.75

# …and how much of an hour must have *elapsed* before its coverage is judged at
# all. At the top of the hour the elapsed window is zero, so every ratio is 0/0:
# without this floor the in-progress hour would be declared an outage on the
# stroke of every hour and clear itself minutes later. The cloud source also
# runs several minutes behind wall clock, so the opening samples of an hour may
# genuinely not have landed yet — the same reason the forecast router refuses to
# project that stretch.
_MIN_COVERAGE_WINDOW_S = 600


@dataclass(frozen=True)
class EnergyHistoryConfig:
    """Sampler + retention knobs, loaded from ``.env`` (all optional)."""

    enabled: bool = True
    persist_interval_s: int = 60
    compact_interval_s: int = 3600
    raw_retention_days: int = 7
    hourly_retention_days: int = 400

    @property
    def raw_retention_seconds(self) -> int:
        return self.raw_retention_days * 24 * _HOUR

    @property
    def hourly_retention_seconds(self) -> int:
        return self.hourly_retention_days * 24 * _HOUR


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_history_config() -> EnergyHistoryConfig:
    """Read the energy-history knobs from ``.env`` (graceful defaults)."""
    load_dotenv(override=True)
    return EnergyHistoryConfig(
        enabled=_env_bool("ENERGY_SAMPLER_ENABLED", True),
        persist_interval_s=max(1, _env_int("ENERGY_PERSIST_INTERVAL_S", 60)),
        compact_interval_s=max(60, _env_int("ENERGY_COMPACT_INTERVAL_S", 3600)),
        raw_retention_days=max(1, _env_int("ENERGY_RAW_RETENTION_DAYS", 7)),
        hourly_retention_days=max(1, _env_int("ENERGY_HOURLY_RETENTION_DAYS", 400)),
    )


# --------------------------------------------------------------- connection
def _connect(path: Optional[Path] = None) -> ContextManager[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection (concurrent sampler write + API read)."""
    return _sqlite_connect(DEFAULT_DB_PATH, path)


def init_db(path: Optional[Path] = None) -> None:
    """Create the tables if they do not exist (idempotent)."""
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts                  INTEGER PRIMARY KEY,
                grid_import_w       REAL,
                grid_export_w       REAL,
                pv_power_w          REAL,
                house_consumption_w REAL,
                pv_surplus_w        REAL,
                meter_reachable     INTEGER NOT NULL DEFAULT 0,
                inverter_reachable  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rollup_hourly (
                hour_start  INTEGER PRIMARY KEY,
                n           INTEGER NOT NULL,
                pv_n        INTEGER NOT NULL,
                pv_wh       REAL,
                house_wh    REAL,
                import_wh   REAL,
                export_wh   REAL,
                pv_avg_w    REAL,
                house_avg_w REAL
            );
            """
        )
        # Added by #579 to an already-deployed table, so it arrives as a
        # migration rather than in the CREATE above. Rows written before it
        # stay NULL and fall back to the coarser sample-count ratio — see
        # :func:`_pv_seconds`.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(rollup_hourly)")}
        if "pv_seconds" not in columns:
            conn.execute("ALTER TABLE rollup_hourly ADD COLUMN pv_seconds REAL")
        conn.commit()


# --------------------------------------------------------------- writes
def record_sample(state: EnergyState, ts: Optional[int] = None, path: Optional[Path] = None) -> None:
    """Persist one live snapshot. ``ts`` defaults to now (epoch seconds)."""
    when = int(ts if ts is not None else time.time())
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO samples (
                ts, grid_import_w, grid_export_w, pv_power_w,
                house_consumption_w, pv_surplus_w, meter_reachable, inverter_reachable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when,
                state.grid_import_w,
                state.grid_export_w,
                state.pv_power_w,
                state.house_consumption_w,
                state.pv_surplus_w,
                1 if state.meter_reachable else 0,
                1 if state.inverter_reachable else 0,
            ),
        )
        conn.commit()


# --------------------------------------------------------------- reads
def recent_samples(minutes: int = 60, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return raw samples from the last ``minutes`` for the live chart.

    Powers are passed through unchanged — ``None`` (e.g. asleep PV) stays
    ``None`` so the client draws a gap, never a 0.
    """
    cutoff = int(time.time()) - max(1, minutes) * 60
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
    return [
        {
            "ts": int(r["ts"]),
            "pv_power_w": r["pv_power_w"],
            "house_consumption_w": r["house_consumption_w"],
            "grid_import_w": r["grid_import_w"],
            "grid_export_w": r["grid_export_w"],
            "pv_surplus_w": r["pv_surplus_w"],
            "inverter_reachable": bool(r["inverter_reachable"]),
            "meter_reachable": bool(r["meter_reachable"]),
        }
        for r in rows
    ]


def _integrate_hour(rows: List[sqlite3.Row], hour_start: int) -> Optional[Dict[str, Any]]:
    """Integrate the samples of one hour into an energy/average rollup dict.

    Energy (Wh) is the rectangular sum of ``power_w * dt`` over consecutive
    in-hour samples, each gap clamped to :data:`_MAX_GAP_SECONDS`. A series
    interval is skipped when its left sample is ``None`` (e.g. asleep PV), so a
    bucket without any PV reading reports ``pv_n = 0`` → ``pv_missing`` upstream.

    ``pv_seconds`` is the width of the intervals the PV integral actually rests
    on — the same ``dt`` values, summed only where the left sample had a PV
    reading (#579). It is what separates "the hour was dim" from "two thirds of
    the hour never arrived": both give a low ``pv_wh``, only the second gives a
    low ``pv_seconds``. Deliberately measured in *seconds of coverage* rather
    than a sample count, so a stretch where the sampler itself was down counts
    as missing just like a stretch where the cloud feed was.
    """
    if not rows:
        return None

    energy = {"pv": 0.0, "house": 0.0, "import": 0.0, "export": 0.0}
    field = {
        "pv": "pv_power_w",
        "house": "house_consumption_w",
        "import": "grid_import_w",
        "export": "grid_export_w",
    }
    pv_sum = 0.0
    pv_n = 0
    pv_seconds = 0.0
    house_sum = 0.0
    house_n = 0

    for i, row in enumerate(rows):
        if row["pv_power_w"] is not None:
            pv_sum += float(row["pv_power_w"])
            pv_n += 1
        if row["house_consumption_w"] is not None:
            house_sum += float(row["house_consumption_w"])
            house_n += 1
        if i + 1 >= len(rows):
            break
        dt = min(int(rows[i + 1]["ts"]) - int(row["ts"]), _MAX_GAP_SECONDS)
        if dt <= 0:
            continue
        for key, col in field.items():
            val = row[col]
            if val is not None:
                energy[key] += float(val) * dt / _HOUR
        if row["pv_power_w"] is not None:
            pv_seconds += dt

    return {
        "hour_start": hour_start,
        "n": len(rows),
        "pv_n": pv_n,
        "pv_seconds": round(pv_seconds, 1),
        "pv_wh": round(energy["pv"], 3),
        "house_wh": round(energy["house"], 3),
        "import_wh": round(energy["import"], 3),
        "export_wh": round(energy["export"], 3),
        "pv_avg_w": round(pv_sum / pv_n, 2) if pv_n else None,
        "house_avg_w": round(house_sum / house_n, 2) if house_n else None,
    }


def compact_and_prune(
    config: Optional[EnergyHistoryConfig] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Fold completed hours of raw samples into ``rollup_hourly``, then prune.

    Only *completed* hours (strictly before the current hour) are rolled up;
    the in-progress hour is left in raw form and computed on the fly by
    :func:`aggregate`. Raw samples past the retention window are deleted; hourly
    rollups past their (much longer) window are deleted too.
    """
    cfg = config or load_history_config()
    current = int(now if now is not None else time.time())
    current_hour = current - (current % _HOUR)

    with _connect(path) as conn:
        # Start just after the last hour already rolled up (or the oldest raw
        # sample on first run).
        last_rolled = conn.execute(
            "SELECT MAX(hour_start) AS h FROM rollup_hourly"
        ).fetchone()["h"]
        oldest = conn.execute("SELECT MIN(ts) AS t FROM samples").fetchone()["t"]
        if oldest is None:
            start_hour = current_hour
        elif last_rolled is None:
            start_hour = int(oldest) - (int(oldest) % _HOUR)
        else:
            start_hour = int(last_rolled) + _HOUR

        hour = start_hour
        rolled = 0
        while hour < current_hour:
            rows = conn.execute(
                "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (hour, hour + _HOUR),
            ).fetchall()
            roll = _integrate_hour(rows, hour)
            if roll is not None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rollup_hourly (
                        hour_start, n, pv_n, pv_wh, house_wh, import_wh,
                        export_wh, pv_avg_w, house_avg_w, pv_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        roll["hour_start"], roll["n"], roll["pv_n"], roll["pv_wh"],
                        roll["house_wh"], roll["import_wh"], roll["export_wh"],
                        roll["pv_avg_w"], roll["house_avg_w"], roll["pv_seconds"],
                    ),
                )
                rolled += 1
            hour += _HOUR

        conn.execute(
            "DELETE FROM samples WHERE ts < ?",
            (current - cfg.raw_retention_seconds,),
        )
        conn.execute(
            "DELETE FROM rollup_hourly WHERE hour_start < ?",
            (current - cfg.hourly_retention_seconds,),
        )
        conn.commit()

    if rolled:
        logger.info("🧮 Compacted %d hour(s) into rollup_hourly", rolled)


def _hourly_since(since: int, now: int, path: Optional[Path]) -> List[Dict[str, Any]]:
    """Hourly buckets with ``hour_start >= since``, oldest first.

    Reads never depend on compaction timing: recent hours (those that still
    have raw samples, i.e. within the raw-retention window) are integrated
    fresh from the raw samples — including the in-progress hour and any
    completed-but-not-yet-compacted hour. Older hours, whose raw samples have
    been pruned, come from ``rollup_hourly``. Raw-derived hours win on overlap.
    """
    with _connect(path) as conn:
        rollups = {
            int(r["hour_start"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM rollup_hourly WHERE hour_start >= ? ORDER BY hour_start ASC",
                (since,),
            ).fetchall()
        }
        raw = conn.execute(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (since,)
        ).fetchall()

    # Group raw samples by their hour and integrate each group fresh.
    by_hour: Dict[int, List[sqlite3.Row]] = {}
    for row in raw:
        hour_start = int(row["ts"]) - (int(row["ts"]) % _HOUR)
        by_hour.setdefault(hour_start, []).append(row)
    for hour_start, group in by_hour.items():
        roll = _integrate_hour(group, hour_start)
        if roll is not None:
            rollups[hour_start] = roll

    return [rollups[k] for k in sorted(rollups)]


def _empty_bucket(key: str, label: str) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "pv_wh": 0.0,
        "house_wh": 0.0,
        "import_wh": 0.0,
        "export_wh": 0.0,
        "n": 0,
        "pv_n": 0,
        "pv_seconds": 0.0,
        "pv_missing": True,
        "partial": False,
    }


def _pv_seconds(hour: Dict[str, Any]) -> float:
    """Seconds of an hourly rollup that its PV integral actually rests on.

    Rollups written before #579 have no ``pv_seconds`` value, and the raw
    samples they were folded from are long pruned, so fall back to the coarser
    sample-count ratio the issue itself proposed (``pv_n / n`` over the hour).
    That is enough to stop already-stored history from reading as a collapse,
    which is the whole point of the flag; it is not enough to distinguish a
    sampler outage from a feed outage, and no stored field can recover that.
    """
    raw = hour.get("pv_seconds")
    if raw is not None:
        return float(raw)
    n = int(hour.get("n") or 0)
    if n <= 0:
        return 0.0
    return _HOUR * min(1.0, int(hour.get("pv_n") or 0) / n)


# The bucket containing *now* has only partly happened, so its totals are still
# climbing towards whatever the slot will finally hold. Left unflagged it reads
# on a chart as a settled value that collapsed — see issue #557. Flagging it
# lets the UI draw it as in-progress, and lets the forecast view project it to a
# full-slot rate.
#
# Deliberately *only* the current slot. This once carried a second claim — that
# a past bucket short because the cloud feed was down "is genuinely that low" —
# which #579 disproved: such a bucket is under-measured, and its shortfall is a
# property of its *coverage*, not of when it happened. That case is now
# `_mark_hourly_coverage`'s, and the two must not be conflated: `partial` means
# "not finished yet", `pv_gap` means "not fully measured".
def _mark_hourly_partial(
    buckets: List[Dict[str, Any]], current: int
) -> List[Dict[str, Any]]:
    """Flag the hourly bucket whose hour contains ``current`` (``key`` = hour start).

    Compares against each bucket's own start rather than rounding ``current`` to
    a 3600-second boundary, because local midnight — where the day frames start
    — need not fall on one in every timezone.
    """
    for b in buckets:
        try:
            start = int(b["key"])
        except (TypeError, ValueError):
            continue
        b["partial"] = start <= current < start + _HOUR
    return buckets


# Coverage is an *hourly* property: only an hour-wide bucket has a denominator
# that is known without guessing. A day bucket's would have to be "the hours the
# sun was up", which this module cannot know — so daily/monthly buckets carry
# the raw `pv_seconds` sum and leave the judgement to whoever has the context.
def _mark_hourly_coverage(
    buckets: List[Dict[str, Any]], current: int
) -> List[Dict[str, Any]]:
    """Add ``pv_coverage`` (0–1) and ``pv_gap`` to one-hour-wide buckets (#579).

    ``pv_coverage`` is the share of the hour whose PV integral rests on real
    data. The in-progress hour is measured against the part of it that has
    *elapsed*, not the full 3600 s — otherwise every current hour would look
    like an outage for 59 minutes.

    ``pv_gap`` is the actionable flag: the hour has some PV data but not enough
    of it (:data:`MIN_TRUSTED_COVERAGE`) for its Wh to be a measurement. Three
    kinds of hour are deliberately excluded from it, each verified against the
    2026-07-30 history the issue was filed from:

    * **No PV data at all** — a sleeping inverter and a whole-hour outage are
      indistinguishable here, and ``pv_missing`` already says "nothing to plot".
    * **No measured generation** (``pv_wh`` 0) — the covered samples all read
      0 W, so there is no evidence of production to be short of and nothing a
      projection could recover (0 scales to 0). Without this the overnight hours
      flag every night: the inverter flaps between 0 W and asleep in the small
      hours, which on 2026-07-30 read as 45% and 10% coverage at 00:00 and
      01:00 — 1.4 h of "outage" in the dark.
    * **Too early to tell** (:data:`_MIN_COVERAGE_WINDOW_S`) — see there.
    """
    for b in buckets:
        try:
            start = int(b["key"])
        except (TypeError, ValueError):
            b["pv_coverage"], b["pv_gap"] = 0.0, False
            continue
        window = _HOUR
        if start <= current < start + _HOUR:
            window = max(0, current - start)
        covered = float(b.get("pv_seconds") or 0.0)
        coverage = min(1.0, covered / window) if window > 0 else 0.0
        b["pv_coverage"] = round(coverage, 3)
        b["pv_gap"] = (
            bool(b["pv_n"])
            and (b.get("pv_wh") or 0.0) > 0
            and window >= _MIN_COVERAGE_WINDOW_S
            and coverage < MIN_TRUSTED_COVERAGE
        )
    return buckets


def _mark_calendar_partial(
    buckets: List[Dict[str, Any]], current: int, fmt: str
) -> List[Dict[str, Any]]:
    """Flag the daily/monthly bucket ``current`` falls in (``key`` = ``strftime(fmt)``)."""
    key_now = datetime.fromtimestamp(current).strftime(fmt)
    for b in buckets:
        b["partial"] = b["key"] == key_now
    return buckets


def _accumulate(bucket: Dict[str, Any], hour: Dict[str, Any]) -> None:
    bucket["pv_wh"] += hour.get("pv_wh") or 0.0
    bucket["house_wh"] += hour.get("house_wh") or 0.0
    bucket["import_wh"] += hour.get("import_wh") or 0.0
    bucket["export_wh"] += hour.get("export_wh") or 0.0
    bucket["n"] += int(hour.get("n") or 0)
    bucket["pv_n"] += int(hour.get("pv_n") or 0)
    bucket["pv_seconds"] += _pv_seconds(hour)
    # Unchanged meaning (#579): *no* PV data at all. Partial coverage is a
    # separate, weaker signal — see :func:`_mark_hourly_coverage` — because
    # `pv_missing` is what `src.tariff` uses to drop a bucket's generation
    # entirely, and an hour that produced 3 measured kWh out of 4 must still
    # contribute its 3.
    bucket["pv_missing"] = bucket["pv_n"] == 0


def _round_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    for k in ("pv_wh", "house_wh", "import_wh", "export_wh"):
        bucket[k] = round(bucket[k], 3)
    bucket["pv_seconds"] = round(bucket["pv_seconds"], 1)
    return bucket


def aggregate(
    period: str = "hourly",
    count: Optional[int] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return chart-ready aggregate buckets for ``hourly``/``daily``/``monthly``.

    Each bucket carries energy (Wh) for PV / house / grid import / export, plus
    ``pv_missing`` (no PV sample landed in the bucket — asleep, not a real 0).
    ``hourly`` buckets additionally carry ``pv_coverage`` / ``pv_gap`` (#579).
    Buckets are local-time, oldest first.
    """
    current = int(now if now is not None else time.time())

    if period == "hourly":
        n = count or 48
        since = current - (current % _HOUR) - (n - 1) * _HOUR
        hours = _hourly_since(since, current, path)
        out = []
        for h in hours[-n:]:
            label = datetime.fromtimestamp(h["hour_start"]).strftime("%H:%M")
            bucket = _empty_bucket(str(h["hour_start"]), label)
            _accumulate(bucket, h)
            out.append(_round_bucket(bucket))
        return _mark_hourly_coverage(_mark_hourly_partial(out, current), current)

    if period == "daily":
        n = count or 30
        since = current - (n + 1) * 24 * _HOUR
        hours = _hourly_since(since, current, path)
        grouped = _group(hours, n, fmt_key="%Y-%m-%d", fmt_label="%a %d")
        return _mark_calendar_partial(grouped, current, "%Y-%m-%d")

    if period == "monthly":
        n = count or 12
        since = current - (n + 1) * 31 * 24 * _HOUR
        hours = _hourly_since(since, current, path)
        grouped = _group(hours, n, fmt_key="%Y-%m", fmt_label="%b %Y")
        return _mark_calendar_partial(grouped, current, "%Y-%m")

    raise ValueError(f"unknown period: {period!r}")


def hourly_range(period: str = "month", now: Optional[int] = None, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Hourly buckets covering one of the five history windows, oldest first.

    Unlike :func:`framed_buckets` (which groups week/month/year to day or month
    resolution for charting), this keeps **hour** resolution so a time-of-use
    tariff can assign each hour to its period. Windows match the chart ranges:
    ``day`` from local midnight, ``week``/``month``/``year`` rolling back 7/30/365
    days, ``total`` all retained history. Each bucket carries ``hour_start`` plus
    the usual pv/house/import/export Wh (see :func:`_hourly_since`).
    """
    current = int(now if now is not None else time.time())
    if period == "day":
        dt_now = datetime.fromtimestamp(current)
        since = int(datetime(dt_now.year, dt_now.month, dt_now.day).timestamp())
    elif period == "week":
        since = current - 7 * 24 * _HOUR
    elif period == "month":
        since = current - 30 * 24 * _HOUR
    elif period == "year":
        since = current - 365 * 24 * _HOUR
    elif period == "total":
        since = 0
    else:
        raise ValueError(f"unknown period: {period!r}")
    return _hourly_since(since, current, path)


def _group(hours: List[Dict[str, Any]], n: int, fmt_key: str, fmt_label: str) -> List[Dict[str, Any]]:
    """Group hourly rollups into day/month buckets (local time), keep last ``n``."""
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for h in hours:
        dt = datetime.fromtimestamp(h["hour_start"])
        key = dt.strftime(fmt_key)
        if key not in buckets:
            buckets[key] = _empty_bucket(key, dt.strftime(fmt_label))
            order.append(key)
        _accumulate(buckets[key], h)
    return [_round_bucket(buckets[k]) for k in order[-n:]]


# --------------------------------------- chart windows (fill-up day + rolling)
def _frame_bucket(key: str, label: str, hours: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build one display bucket from a (possibly empty) list of hourly rollups."""
    bucket = _empty_bucket(key, label)
    for h in hours:
        _accumulate(bucket, h)
    return _round_bucket(bucket)


def framed_buckets(
    period: str = "day",
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Chart-ready energy buckets for the history view's five ranges.

    * ``day``   — 24 hourly slots from local midnight today, *padded* so future
      hours come back empty and the chart fills left-to-right through the day.
    * ``week``  — rolling last 7 days (daily buckets).
    * ``month`` — rolling last 30 days (daily buckets).
    * ``year``  — rolling last 12 months (monthly buckets; ~365 days, but month
      resolution stays readable where 365 daily points would not).
    * ``total`` — every month of retained history.

    Rolling ranges reuse :func:`aggregate` (last-N, data-only — never a sparse
    empty frame); only ``day`` is a fixed fill-up window. Each bucket carries
    generation (``pv_wh``) / grid-supplied (``import_wh``) / consumption
    (``house_wh``) Wh.
    """
    current = int(now if now is not None else time.time())

    if period == "day":
        dt_now = datetime.fromtimestamp(current)
        start = datetime(dt_now.year, dt_now.month, dt_now.day)
        since = int(start.timestamp())
        by_hour = {int(h["hour_start"]): h for h in _hourly_since(since, current, path)}
        out = []
        for i in range(24):
            hs = since + i * _HOUR
            hours = [by_hour[hs]] if hs in by_hour else []
            out.append(_frame_bucket(str(hs), "%02d" % i, hours))
        return _mark_hourly_coverage(_mark_hourly_partial(out, current), current)

    if period == "week":
        return aggregate("daily", 7, now, path)

    if period == "month":
        return aggregate("daily", 30, now, path)

    if period == "year":
        return aggregate("monthly", 12, now, path)

    if period == "total":
        ordered: List[str] = []
        groups: Dict[str, List[Dict[str, Any]]] = {}
        labels: Dict[str, str] = {}
        for h in _hourly_since(0, current, path):
            d = datetime.fromtimestamp(h["hour_start"])
            key = d.strftime("%Y-%m")
            if key not in groups:
                groups[key] = []
                labels[key] = d.strftime("%b %y")
                ordered.append(key)
            groups[key].append(h)
        months = [_frame_bucket(k, labels[k], groups[k]) for k in ordered]
        return _mark_calendar_partial(months, current, "%Y-%m")

    raise ValueError(f"unknown period: {period!r}")


def hourly_day(
    offset_days: int = 0,
    now: Optional[int] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """24 padded hourly buckets for a single local day (``offset_days`` from today).

    ``0`` is today, ``-1`` yesterday, ``+1`` tomorrow. Like the ``day`` branch of
    :func:`framed_buckets` but for any day, so the solar-forecast view (issue #39)
    can overlay a past day's *actual* generation against the forecast. Hours with
    no sample (a future day, or before the app first ran) come back as empty 0-Wh
    buckets so the result is always a continuous 24-slot frame.
    """
    current = int(now if now is not None else time.time())
    dt_now = datetime.fromtimestamp(current)
    start = datetime(dt_now.year, dt_now.month, dt_now.day) + timedelta(days=offset_days)
    since = int(start.timestamp())
    end = since + 24 * _HOUR
    by_hour = {
        int(h["hour_start"]): h
        for h in _hourly_since(since, min(current, end), path)
    }
    out = []
    for i in range(24):
        hs = since + i * _HOUR
        hours = [by_hour[hs]] if hs in by_hour else []
        out.append(_frame_bucket(str(hs), "%02d" % i, hours))
    return _mark_hourly_coverage(_mark_hourly_partial(out, current), current)
