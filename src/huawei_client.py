"""
Huawei FusionSolar energy client
================================
Non-UI core: read the home's live energy flow, ahead of the solar
load-balancing automation (shift HVAC load to match PV).

Source: the **FusionSolar cloud portal** (`fusionsolar.huawei.com`), read with
the plant account's own credentials. One call returns the whole flow — PV
generation, house consumption, and the grid exchange measured by the inverter's
RS485 power sensor — so there is no separate meter/inverter split and no local
network dependency.

Why cloud and not local Modbus
------------------------------
The installed **SUN2000-8K-LC0** exposes no usable Modbus interface: TCP 502 is
refused on both the LAN address and the inverter's own access point, and the
proprietary port 6607 accepts a connection but answers no Modbus request (it is
TLS-wrapped on this generation). Verified down to the wire protocol, including
against the ``huawei-solar`` library. Enabling it needs an ``SDongleA-05``
fitted by the installer; until then the cloud API is the only complete source.
See the investigation write-up referenced from ``README.md``.

Sign convention
---------------
FusionSolar reports a single signed ``meterActivePower`` rather than separate
import/export figures, and its sign is the **opposite** of the one the portal's
own device page shows for the meter. The identity that pins it down, confirmed
against live values::

    productPower (2.660 kW) + meterActivePower (0.565 kW) = usePower (3.225 kW)

So ``meterActivePower`` is **positive when importing** from the grid and
negative when exporting. ``grid_import_w`` / ``grid_export_w`` are split out of
it here, which keeps the sign logic in exactly one place.

Wraps ``fusion_solar_py``, which is synchronous, so every call is dispatched to
a worker thread. Shared by the CLI (``src/list_energy.py``) and the webapp
(``GET /api/energy``) so the device-access logic lives in exactly one place.

Config (from ``.env``):

* ``FUSIONSOLAR_USER`` / ``FUSIONSOLAR_PASSWORD`` — FusionSolar portal account
* ``FUSIONSOLAR_SUBDOMAIN`` — regional host prefix (default ``uni005eu5``)
* ``FUSIONSOLAR_PLANT_DN`` — plant DN, e.g. ``NE=314891536`` (optional; the
  account's first plant is discovered automatically when blank)
* ``FUSIONSOLAR_MAX_STALENESS_S`` — discard a cloud point older than this many
  seconds (default 900), so a frozen upload cannot masquerade as live data
* ``FUSIONSOLAR_CACHE_TTL_S`` — reuse one cloud response for this many seconds
  (default 60). The portal's own resolution is 5 minutes, so polling it harder
  buys nothing and risks being throttled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger("huawei")

_DEFAULT_SUBDOMAIN = "uni005eu5"

# How old the newest usable point may be before the read is reported
# unavailable. Same guard, and the same reasoning, as the integration this
# replaced (issue #94) — but a good deal more generous, because this source
# behaves differently. Measured over one evening: the series runs ~7 minutes
# behind wall clock, and it emits *permanent* holes of 2–3 consecutive buckets
# (18:25-18:30 and 19:15-19:25 were still unusable an hour later). The newest
# trustworthy point is therefore routinely ~25 minutes old with nothing wrong,
# and a 15-minute window flapped to "unavailable" during normal operation.
# FusionSolar's own app falls back the same way rather than blanking — at 18:30
# it was showing the 18:20 values. This guard is for a *stopped* feed, not a gap.
_DEFAULT_MAX_STALENESS_S = 1800

# How long one cloud response is reused. The portal publishes on a 5-minute
# grid, so a request-per-poll is pure waste: the PWA polls the energy tab every
# 5 s, which was one cloud round-trip each, serialising behind the session lock
# and slowing every caller. 60 s loses no resolution at all.
_DEFAULT_CACHE_TTL_S = 60

# FusionSolar's balance-series timestamps are naive local, "2026-07-28 17:50".
_TIME_FORMAT = "%Y-%m-%d %H:%M"

# The live-flow series, all sampled on the same 5-minute grid. Read as one
# aligned point so the snapshot balances — see :func:`_latest_aligned`.
_FLOW_KEYS = ("productPower", "usePower", "meterActivePower")

# How far a bucket may stray from ``product + meter == use`` before it is
# treated as half-written. Settled buckets satisfy the identity *exactly*, and
# the series carry 3 decimals of kW, so this only has to absorb rounding of the
# three terms (worst case ~0.0015 kW). Keep it tight: a first attempt at 0.05
# let a half-written 19:20 bucket through — PV 1.121, meter -1.143, use 0.000
# is only 22 W off, yet it rendered the house at 0 W and the grid exporting
# more than the array generated. The gap a placeholder leaves shrinks to zero
# as PV approaches export in the evening, so a generous window is exactly wrong.
_FLOW_IDENTITY_TOLERANCE_KW = 0.005

# How long to stay quiet after a failed cloud read. A failure leaves nothing to
# put in the response cache below, so without this guard *every* request
# re-attempts the login — and the PWA polls energy every 5 s. Observed live: an
# expired session turned into a login storm, the tray re-attempting every ~17 s
# for minutes, after which the portal refused it outright while a fresh login
# from a one-shot CLI still worked. Doubling backoff to a ceiling keeps a
# transient outage from escalating into a locked account.
_FAILURE_BACKOFF_BASE_S = 60
_FAILURE_BACKOFF_MAX_S = 900


@dataclass
class EnergyState:
    """Flattened snapshot of the home's instantaneous energy flow.

    All powers are in watts, signed from the house's point of view where it
    matters: ``pv_surplus_w`` is positive when exporting (PV covers the load
    with power to spare — the signal to shift more HVAC load on) and negative
    when importing from the grid.  ``None`` means "not measured right now"
    (e.g. PV while the inverter is asleep) — never silently coerced to 0.
    """

    grid_import_w: Optional[float] = None
    grid_export_w: Optional[float] = None
    pv_power_w: Optional[float] = None
    house_consumption_w: Optional[float] = None
    pv_surplus_w: Optional[float] = None
    # Cumulative counters are **today's** totals, not lifetime ones: that is
    # what FusionSolar exposes. They reset at midnight, which Home Assistant's
    # TOTAL_INCREASING state class handles natively.
    grid_import_kwh: Optional[float] = None
    grid_export_kwh: Optional[float] = None
    meter_reachable: bool = False
    inverter_reachable: bool = False
    meter_serial: Optional[str] = None
    #: The 5-minute bucket this snapshot was read from, as the portal's own
    #: naive-local ``"YYYY-MM-DD HH:MM"`` stamp — ``None`` when nothing usable
    #: was read. Not part of the ``/api/energy`` response; it exists so the HVAC
    #: boost coordinator can tell a *fresh* reading from the same bucket it
    #: already acted on (issue #562). Wall-clock cannot: this source runs several
    #: minutes behind and emits permanent multi-bucket holes, so "300 s have
    #: passed" is not "the meter has published again".
    as_of: Optional[str] = None


@dataclass(frozen=True)
class EnergyConfig:
    """Runtime FusionSolar config loaded from ``.env``."""

    user: Optional[str]
    password: Optional[str]
    subdomain: str
    plant_dn: Optional[str]
    max_staleness_s: int = _DEFAULT_MAX_STALENESS_S
    cache_ttl_s: int = _DEFAULT_CACHE_TTL_S


# One portal session is reused across reads: logging in per request would be
# both slow and a good way to get rate-limited. The lock keeps concurrent
# callers (webapp request + background sampler) from racing to re-login.
_client: Any = None
_client_lock: Optional[asyncio.Lock] = None
_plant_dn: Optional[str] = None

# (monotonic timestamp, day-series payload) — see :func:`_fetch_stats`.
_stats_cache: Optional[tuple[float, Dict[str, Any]]] = None

# Monotonic deadline before which no cloud call is attempted, and the number of
# consecutive failures that set it — see :func:`_note_failure`.
_failure_until: float = 0.0
_failure_streak: int = 0


# Key of the last state line emitted — see :func:`_log_state`.
_last_log_key: Optional[tuple] = None


def _log_state(level: int, key: tuple, msg: str, *args: Any) -> None:
    """Log once per distinct ``key``, dropping repeats to debug.

    Callers poll this module far faster than the source publishes — the PWA
    asks every 5 s against a 5-minute grid — so an unconditional line per read
    writes the same sentence hundreds of times an hour. Only the first sighting
    of a given bucket carries information; the repeats are noise that would
    bury the lines worth finding.
    """
    global _last_log_key

    if key == _last_log_key:
        logger.debug(msg, *args)
        return
    _last_log_key = key
    logger.log(level, msg, *args)


def _backoff_for(streak: int) -> int:
    """Seconds to stay quiet after ``streak`` consecutive failures."""
    if streak < 1:
        return 0
    return min(_FAILURE_BACKOFF_MAX_S, _FAILURE_BACKOFF_BASE_S * 2 ** (streak - 1))


def _note_failure(now: float) -> None:
    """Open (or lengthen) the backoff window after a failed read."""
    global _failure_until, _failure_streak

    _failure_streak += 1
    wait = _backoff_for(_failure_streak)
    _failure_until = now + wait
    logger.warning(
        "⚠️ FusionSolar unavailable (%d consecutive failure(s)); "
        "not retrying for %ds",
        _failure_streak,
        wait,
    )


def _note_success() -> None:
    """Clear the backoff after a good read."""
    global _failure_until, _failure_streak

    if _failure_streak:
        logger.info("✅ FusionSolar recovered after %d failure(s)", _failure_streak)
    _failure_streak = 0
    _failure_until = 0.0


def _get_lock() -> asyncio.Lock:
    """Return the module lock, created lazily on the running loop."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


def _load_config() -> EnergyConfig:
    """Read FusionSolar settings from ``.env``.

    Missing credentials are not an error: the read simply reports nothing
    reachable, exactly as an unreachable device would.
    """
    load_dotenv(override=True)
    user = (os.getenv("FUSIONSOLAR_USER") or "").strip() or None
    password = (os.getenv("FUSIONSOLAR_PASSWORD") or "").strip() or None
    subdomain = (
        os.getenv("FUSIONSOLAR_SUBDOMAIN") or _DEFAULT_SUBDOMAIN
    ).strip() or _DEFAULT_SUBDOMAIN
    plant_dn = (os.getenv("FUSIONSOLAR_PLANT_DN") or "").strip() or None

    raw_staleness = (os.getenv("FUSIONSOLAR_MAX_STALENESS_S") or "").strip()
    try:
        max_staleness_s = (
            int(raw_staleness) if raw_staleness else _DEFAULT_MAX_STALENESS_S
        )
    except ValueError:
        logger.warning(
            "⚠️ Invalid FUSIONSOLAR_MAX_STALENESS_S=%s; using %s",
            raw_staleness, _DEFAULT_MAX_STALENESS_S,
        )
        max_staleness_s = _DEFAULT_MAX_STALENESS_S

    raw_ttl = (os.getenv("FUSIONSOLAR_CACHE_TTL_S") or "").strip()
    try:
        cache_ttl_s = int(raw_ttl) if raw_ttl else _DEFAULT_CACHE_TTL_S
    except ValueError:
        logger.warning(
            "⚠️ Invalid FUSIONSOLAR_CACHE_TTL_S=%s; using %s",
            raw_ttl, _DEFAULT_CACHE_TTL_S,
        )
        cache_ttl_s = _DEFAULT_CACHE_TTL_S

    return EnergyConfig(
        user, password, subdomain, plant_dn, max_staleness_s, cache_ttl_s
    )


def _is_stale(payload_time: object, max_staleness_s: int) -> bool:
    """True if the cloud point's as-of timestamp is older than the freshness window.

    When the inverter stops uploading, the portal keeps serving the last point
    unchanged, so a frozen value looks live and would flat-line the chart. A
    missing or unparseable timestamp is treated as fresh — staleness cannot be
    proven, so an otherwise-good read is not discarded.
    """
    if not payload_time:
        return False
    try:
        as_of = datetime.strptime(str(payload_time), _TIME_FORMAT)
        age = (datetime.now() - as_of).total_seconds()
    except (ValueError, TypeError):
        logger.debug("FusionSolar time %r unparseable; treating as fresh", payload_time)
        return False
    return age > max_staleness_s


def _as_float(value: object) -> Optional[float]:
    """Convert API numbers to float while preserving missing values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _series_float(raw: object) -> Optional[float]:
    """Convert one series sample, treating FusionSolar's ``--`` filler as missing."""
    if raw is None or (isinstance(raw, str) and raw.strip() in {"", "--"}):
        return None
    return _as_float(raw)


def _is_settled(point: Dict[str, float]) -> bool:
    """True if a bucket satisfies ``productPower + meterActivePower == usePower``.

    FusionSolar fills the newest bucket *progressively*, and a half-written one
    is not marked ``--``: observed live at 18:25 with ``productPower=2.054``,
    ``meterActivePower=-2.167`` and ``usePower`` still at a placeholder
    ``0.000`` — which would render the house at 0 W and the grid exporting
    2,167 W from a 2,054 W array, i.e. more than was generated.

    Consumption is a *derived* series on this plant (``existUsePower`` is
    ``False``), which is why it settles last. Checking the identity is therefore
    the honest completeness test: a bucket that does not balance is not yet
    real, whatever its individual fields say.

    The rule is tight because it can afford to be: across a full day of live
    data, 32 of 39 buckets had a residual of *exactly* zero, and all 7 that did
    not were placeholders. Each of those also failed physically — the meter
    claimed more export than the array generated, impossible with no battery —
    so the identity doubles as a plausibility check on the meter itself.

    Unverifiable (no meter, or no consumption series) counts as settled — the
    check cannot prove the bucket bad, so a usable read is not thrown away.
    """
    pv = point.get("productPower")
    use = point.get("usePower")
    meter = point.get("meterActivePower")
    if pv is None or use is None or meter is None:
        return True
    return abs(pv + meter - use) <= _FLOW_IDENTITY_TOLERANCE_KW


def _settled_buckets(stats: Dict[str, Any]) -> List[tuple[str, Dict[str, float]]]:
    """Every complete, balancing 5-minute bucket of the day, oldest first.

    The day arrives as parallel 288-slot arrays (``xAxis`` plus one per series),
    with ``--`` for slots that have not been filled. Two distinct hazards, both
    seen live, are handled here:

    * The library's own ``get_last_plant_data`` takes the last non-``--`` sample
      of *each series independently*, silently mixing buckets when one series
      lags another (18:05 vs 18:10 — ~170 W out of balance). Reading a whole
      bucket at one index fixes that.
    * A bucket may be half-written, with a placeholder rather than ``--`` (see
      :func:`_is_settled`). Such buckets are skipped entirely.

    Series empty for the whole day (e.g. no power sensor fitted) are excluded
    from the alignment rather than blocking it.
    """
    x_axis = stats.get("xAxis") or []
    series = {key: (stats.get(key) or []) for key in _FLOW_KEYS}
    # Only require series that reported at all today; an absent device must not
    # stop the others from being read.
    present = {
        key: values for key, values in series.items()
        if any(_series_float(v) is not None for v in values)
    }
    if not present:
        return []

    buckets: List[tuple[str, Dict[str, float]]] = []
    length = min([len(x_axis)] + [len(v) for v in present.values()])
    for index in range(length):
        point = {key: _series_float(values[index]) for key, values in present.items()}
        if not all(value is not None for value in point.values()):
            continue
        settled = {k: v for k, v in point.items() if v is not None}
        if not _is_settled(settled):
            logger.debug(
                "FusionSolar bucket %s not settled (%s); skipping",
                x_axis[index], settled,
            )
            continue
        buckets.append((x_axis[index], settled))
    return buckets


def _latest_aligned(stats: Dict[str, Any]) -> tuple[Optional[str], Dict[str, float]]:
    """Return the newest settled bucket, or ``(None, {})`` if there is none."""
    buckets = _settled_buckets(stats)
    return buckets[-1] if buckets else (None, {})


def _latest_pv(stats: Dict[str, Any]) -> tuple[Optional[str], Optional[float]]:
    """Return the newest PV reading, ignoring meter consistency entirely.

    ``productPower`` comes straight off the inverter and is self-consistent even
    when the power sensor is talking nonsense, so it survives a meter fault that
    invalidates the rest of the bucket. Used only for the degraded read in
    :func:`fetch_energy_state`.
    """
    x_axis = stats.get("xAxis") or []
    values = stats.get("productPower") or []
    for index in range(min(len(x_axis), len(values)) - 1, -1, -1):
        pv = _series_float(values[index])
        if pv is not None:
            return x_axis[index], pv
    return None, None


def _state_from_point(point: Dict[str, float]) -> EnergyState:
    """Build an :class:`EnergyState` from one settled bucket's kW values."""
    state = EnergyState()
    pv_kw = point.get("productPower")
    use_kw = point.get("usePower")
    meter_kw = point.get("meterActivePower")

    if pv_kw is not None:
        state.pv_power_w = round(pv_kw * 1000, 1)
    if use_kw is not None:
        state.house_consumption_w = round(use_kw * 1000, 1)

    # Split the single signed meter figure into the two fields the rest of the
    # app expects. Positive = importing (see the module docstring's identity).
    if meter_kw is not None:
        watts = meter_kw * 1000
        state.grid_import_w = round(max(watts, 0.0), 1)
        state.grid_export_w = round(max(-watts, 0.0), 1)

    state.meter_reachable = meter_kw is not None
    state.inverter_reachable = pv_kw is not None
    _derive(state)
    return state


def _connect(config: EnergyConfig) -> Any:
    """Create a logged-in portal client (blocking; runs in a worker thread)."""
    from fusion_solar_py.client import FusionSolarClient

    return FusionSolarClient(
        config.user, config.password, huawei_subdomain=config.subdomain
    )


async def _get_client(config: EnergyConfig) -> Any:
    """Return a live portal client, logging in or re-logging in as needed."""
    global _client, _plant_dn

    if not config.user or not config.password:
        logger.info("ℹ️ FusionSolar credentials not set; skipping energy read")
        return None

    async with _get_lock():
        if _client is not None:
            try:
                if await asyncio.to_thread(_client.is_session_active):
                    return _client
            except Exception as exc:  # noqa: BLE001 - any transport error
                logger.info("ℹ️ FusionSolar session check failed (%s); re-logging in", exc)
            _client = None
            _plant_dn = None

        try:
            _client = await asyncio.to_thread(_connect, config)
        except Exception as exc:  # noqa: BLE001 - bad creds, portal down, captcha
            logger.warning("⚠️ FusionSolar login failed: %s", exc)
            _client = None
            return None
        logger.info("✅ FusionSolar login OK (%s)", config.subdomain)
        return _client


async def _resolve_plant_dn(client: Any, config: EnergyConfig) -> Optional[str]:
    """Return the plant DN to read, discovering it once when not configured."""
    global _plant_dn

    if config.plant_dn:
        return config.plant_dn
    if _plant_dn:
        return _plant_dn
    try:
        plant_ids = await asyncio.to_thread(client.get_plant_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Could not list FusionSolar plants: %s", exc)
        return None
    if not plant_ids:
        logger.warning("⚠️ FusionSolar account has no plants")
        return None
    _plant_dn = plant_ids[0]
    logger.info("ℹ️ Using FusionSolar plant %s", _plant_dn)
    return _plant_dn


def _read_plant(client: Any, plant_dn: str) -> Dict[str, Any]:
    """Fetch the plant's whole-day series (blocking; runs in a worker thread)."""
    return client.get_plant_stats(plant_dn)


async def _fetch_stats(config: EnergyConfig) -> Optional[Dict[str, Any]]:
    """Return the plant's day series, reusing a recent response when possible.

    Every caller — the live tile, the history sampler, the backfill — wants the
    same payload, and the portal only publishes every 5 minutes. Serving them
    from one cached response keeps the cloud call count proportional to time
    rather than to traffic, and stops concurrent callers queueing behind the
    session lock.
    """
    global _stats_cache, _client, _plant_dn

    now = time.monotonic()
    cached = _stats_cache
    if cached is not None and now - cached[0] < config.cache_ttl_s:
        return cached[1]

    if not config.user or not config.password:
        # Not configured is not a failure: nothing to back off from, and the
        # login path already says so once per call at info level.
        return None

    if now < _failure_until:
        # Backing off. Serve the last good payload rather than nothing — its own
        # as-of timestamp still gates freshness downstream, so a genuinely dead
        # feed is reported unavailable by the staleness guard, not by silence.
        return cached[1] if cached is not None else None

    client = await _get_client(config)
    if client is None:
        _note_failure(now)
        return None

    plant_dn = await _resolve_plant_dn(client, config)
    if plant_dn is None:
        # A cached client that fails past login is worse than none: _get_client
        # reuses it as long as its own is_session_active() check keeps saying
        # yes, even after the portal has poisoned it. Drop it so the next
        # attempt after backoff builds a genuinely fresh client, not a repeat
        # of the same broken one (#556).
        _client = None
        _plant_dn = None
        _note_failure(now)
        return None

    try:
        stats = await asyncio.to_thread(_read_plant, client, plant_dn)
    except Exception as exc:  # noqa: BLE001 - portal error, schema change, timeout
        logger.warning("⚠️ FusionSolar read failed: %s", exc)
        # Same reasoning as above: a read failure this deep (past the client's
        # own is_session_active() check) means the cached client is bad, not
        # just the request. Force a fresh client/session on the next try (#556).
        _client = None
        _plant_dn = None
        _note_failure(now)
        return None

    if not isinstance(stats, dict):
        logger.warning("⚠️ FusionSolar returned an unexpected payload: %r", type(stats))
        _note_failure(now)
        return None

    _note_success()
    _stats_cache = (now, stats)
    return stats


def _apply(state: EnergyState, stats: Dict[str, Any]) -> Optional[str]:
    """Map a FusionSolar payload onto ``state``; return its as-of timestamp."""
    as_of, point = _latest_aligned(stats)
    latest = _state_from_point(point)

    state.pv_power_w = latest.pv_power_w
    state.house_consumption_w = latest.house_consumption_w
    state.grid_import_w = latest.grid_import_w
    state.grid_export_w = latest.grid_export_w
    state.pv_surplus_w = latest.pv_surplus_w

    # Cumulative counters are today's totals, not lifetime ones.
    state.grid_import_kwh = _as_float(stats.get("totalBuyPower"))
    state.grid_export_kwh = _as_float(stats.get("totalOnGridPower"))

    state.meter_reachable = bool(stats.get("existMeter")) and latest.meter_reachable
    state.inverter_reachable = (
        bool(stats.get("existInverter")) and latest.inverter_reachable
    )
    state.as_of = as_of

    return as_of


def _derive(state: EnergyState) -> None:
    """Fill the computed fields from the raw reads."""
    imp = state.grid_import_w
    exp = state.grid_export_w
    if imp is None or exp is None:
        return
    # Signed net export: + when PV covers the load with power to spare,
    # - when drawing from the grid. One of import/export is always 0.
    state.pv_surplus_w = round(exp - imp, 1)
    if state.house_consumption_w is None and state.pv_power_w is not None:
        # consumption = production + what we pull − what we push back
        state.house_consumption_w = round(state.pv_power_w + imp - exp, 1)


async def fetch_energy_state() -> EnergyState:
    """Read the live FusionSolar energy flow and return a flattened snapshot.

    Never raises for missing credentials, a portal outage, or a stale upload —
    partial or empty data is a normal, useful result.  The reachability flags
    say what was actually read.

    **Degrades to PV-only rather than to nothing.** A power-sensor fault
    invalidates the grid and consumption figures but says nothing about the
    inverter's own reading, so a bucket that fails the consistency check still
    yields usable solar output. Observed live on the day of commissioning: from
    19:15 the portal published ``load 0.000 kW`` with 1.97 kW of export from a
    0.77 kW array — FusionSolar's own app rendered those impossible numbers, and
    the fault persisted for over half an hour. Blanking the whole tile for that
    would also be a step back from the integration this replaced, which read the
    meter and the inverter as independent sources.
    """
    config = _load_config()
    state = EnergyState()

    stats = await _fetch_stats(config)
    if stats is None:
        return state

    as_of = _apply(state, stats)

    if _is_stale(as_of, config.max_staleness_s):
        # A frozen point keeps coming back unchanged and would be recorded as a
        # fresh live sample, flat-lining the chart. Fall back to whatever the
        # inverter alone can still tell us rather than lying about liveness.
        pv_as_of, pv_kw = _latest_pv(stats)
        if pv_kw is not None and not _is_stale(pv_as_of, config.max_staleness_s):
            _log_state(
                logging.WARNING,
                ("degraded", as_of, pv_as_of),
                "⚠️ FusionSolar flow unusable (newest consistent point %s) — "
                "serving PV only from %s; the power sensor is reporting "
                "impossible values",
                as_of, pv_as_of,
            )
            degraded = EnergyState()
            degraded.pv_power_w = round(pv_kw * 1000, 1)
            degraded.inverter_reachable = True
            degraded.meter_reachable = False
            return degraded

        _log_state(
            logging.WARNING,
            ("stale", as_of),
            "⚠️ FusionSolar data stale (as-of %s, older than %ds) — reporting unavailable",
            as_of, config.max_staleness_s,
        )
        return EnergyState()

    _derive(state)
    _log_state(
        logging.INFO,
        ("ok", as_of),
        "✅ FusionSolar %s (as-of %s): PV %s W, import %s W, export %s W, load %s W",
        config.plant_dn or _plant_dn, as_of, state.pv_power_w, state.grid_import_w,
        state.grid_export_w, state.house_consumption_w,
    )
    return state


async def fetch_energy_day() -> List[tuple[int, EnergyState]]:
    """Return today's whole 5-minute series as ``(epoch_seconds, state)`` pairs.

    The portal serves the entire day on every read, so this costs no extra API
    call beyond the one :func:`fetch_energy_state` already makes — it simply
    stops discarding the other 287 buckets.

    This exists so the history can be **backfilled**: the app's charts and kWh
    cards integrate its own persisted samples, so a day on which the app was
    not running (or, as when this client replaced the SMA one, was reading
    hardware that no longer existed) leaves a hole that never fills in. Writes
    are keyed by timestamp and idempotent, so replaying a day is safe.

    Never raises; an unreachable portal yields an empty list.
    """
    config = _load_config()

    stats = await _fetch_stats(config)
    if stats is None:
        return []

    out: List[tuple[int, EnergyState]] = []
    for when, point in _settled_buckets(stats):
        try:
            # Series timestamps are naive local, matching this host's clock.
            ts = int(datetime.strptime(when, _TIME_FORMAT).timestamp())
        except (ValueError, TypeError):
            continue
        out.append((ts, _state_from_point(point)))
    return out
