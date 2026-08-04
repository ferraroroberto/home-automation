r"""
Huawei SUN2000 local Modbus TCP client
======================================
Non-UI core: read the home's live energy flow **locally**, off the SDongleA-05
smart dongle wired to the LAN, instead of waiting on the FusionSolar cloud.

This is the primary source for :func:`src.huawei_client.fetch_energy_state`;
the cloud path stays as the automatic fallback. The win is resolution: the
registers answer in about a second, where the portal publishes on a 5-minute
grid and runs several minutes behind wall clock on top of that.

Sign convention — the opposite of the cloud's
---------------------------------------------
Register ``37113`` (the power sensor's active power) is **positive when
exporting** to the grid. That is the *reverse* of the cloud's
``meterActivePower``, which :mod:`src.huawei_client` documents as positive when
importing — so the two sources must never share a split.

Proven on the live system (2026-08-04 18:10-18:15), not inferred:

* The cloud reported **exporting 1004 W** at 18:10 (its own convention already
  pinned by the ``product + meter == use`` identity). Two minutes later
  ``37113`` read **+1015 W**, with PV agreeing — same sign question, 1%
  magnitude agreement.
* Over a 220-second window with ``37113`` positive throughout, the meter's
  cumulative register ``37119`` advanced 0.05 kWh (≈0.81 kW average, matching
  the ~0.9 kW instantaneous reading) while ``37121`` stayed frozen. The counter
  that moves under a positive reading is therefore the export counter.

The issue that specified this work (#618) proposed settling it after dark, when
zero PV forces an unambiguous import. The cross-check above is strictly
stronger — it fixes the sign *and* the magnitude against a convention this repo
had already proven — so it was used instead of waiting for nightfall.

The split into ``grid_import_w`` / ``grid_export_w`` happens in exactly one
place, :func:`_state_from_registers`, mirroring the single split site in
:mod:`src.huawei_client`.

Which register is "PV production"
---------------------------------
``32080`` (inverter **AC** active power), not ``32064`` (DC input power). Two
reasons, and they agree:

* Only the AC figure balances at the meter — ``active_power - export == house
  load`` is what physically flows through the coupling point. Using the DC
  figure would overstate house consumption by the inverter's conversion loss
  (~2%, about 70 W at 3.4 kW) on every single reading.
* It is what the cloud already means by ``productPower``, so the persisted
  history does not step when the serving source changes. Checked against the
  day's energy rather than a lagged instantaneous sample: register ``32114``
  (daily yield) read 35.1 kWh while the cloud's own ``productPower`` series
  integrated to 34.98 kWh over the same day.

Operating constraints (Huawei's SDongleA-05 MODBUS TCP Guide, plus what the
hardware actually did while this was being written)
---------------------------------------------------------------------------
* **One client at a time.** "If unrestricted is enabled, all client devices on
  the same LAN can access the network, but only one client device can access
  the network at a time." Opening a second session while a first was reading
  dropped the first with a ``ConnectionException`` mid-read — observed, not
  theoretical. Hence :data:`_read_lock`, and hence connect → read → close on
  every cycle rather than holding a session open. This app must be the single
  collector: Home Assistant consumes ``/api/energy``, never the dongle.
* **The dongle is slow.** A full register sweep takes ~20 s and sub-500 ms
  timeouts never return data, so the timeout floor is 1 s and only two small
  register blocks are read.
* **The dongle self-reboots** when it loses its route to the gateway or the
  FusionSolar cloud, which kills the Modbus session. A cloud outage therefore
  takes this local path down with it — which is exactly why the cloud fallback
  is kept rather than deleted.
* **Address by MAC, not IP.** The dongle holds a DHCP lease and moves; it had
  already drifted .88 → .108 between the issue being written and this being
  built. ``HUAWEI_MODBUS_HOST`` is a cold-start hint, never the source of
  truth — see :func:`_resolve_host`.

Config (from ``.env``):

* ``HUAWEI_MODBUS_MAC`` — the dongle's MAC; the authoritative address
* ``HUAWEI_MODBUS_HOST`` — last-known IP, a cold-start hint only
* ``HUAWEI_MODBUS_PORT`` — default 502
* ``HUAWEI_MODBUS_UNIT_ID`` — Modbus device id, default 1
* ``HUAWEI_MODBUS_TIMEOUT_S`` — per-operation timeout, floored at 1 s
* ``HUAWEI_MODBUS_CACHE_TTL_S`` — reuse one snapshot for this long (default 5),
  which is what holds the poll cadence at ~5 s no matter how many callers ask

With neither a MAC nor a host configured the client is simply disabled and
:func:`fetch_modbus_state` returns ``None``, so the cloud serves — which is also
what keeps CI off the real inverter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from src.huawei_client import EnergyState, _derive

logger = logging.getLogger("huawei.modbus")

# pymodbus reports every failed connect at ERROR level. This module already
# reports the same failure with context, and only when the serving source
# actually changes — so the library's copy is a duplicate that would scream once
# a minute through an outage the app is handling cleanly, and once on every
# startup where the .env host hint has gone stale (the normal, self-healing case).
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

_DEFAULT_PORT = 502
_DEFAULT_UNIT_ID = 1

# Floored at 1 s deliberately: the dongle answers in its own time and anything
# under ~500 ms simply never returns data (see the module docstring).
_DEFAULT_TIMEOUT_S = 3.0
_MIN_TIMEOUT_S = 1.0

# One snapshot is reused for this long. The PWA polls the energy tab every 5 s
# and the sampler and boost coordinator read on their own cadences; without
# this they would queue behind the read lock and hammer a device that tolerates
# exactly one client. This *is* the "~5 s between reads" the dongle wants.
_DEFAULT_CACHE_TTL_S = 5

# How long to stop trying after a failed read, so a dropped dongle degrades to
# the cloud path cleanly instead of paying a connect timeout on every request.
_FAILURE_BACKOFF_S = 60.0

# Ceiling on one whole read cycle (connect + two block reads + close), so a
# half-open socket cannot outlive the caller's own budget.
_READ_BUDGET_S = 15.0

# --- Register map (Huawei SUN2000 / Smart Power Sensor, holding registers) ---
# Read as two small contiguous blocks rather than per-register: the dongle is
# slow, and both spans were verified to answer as a block on the real hardware.
_INVERTER_START = 32064          # PV input power (int32, W)
_INVERTER_COUNT = 18             # ... through 32081
_ACTIVE_POWER_OFFSET = 16        # 32080 — inverter AC active power (int32, W)

_METER_START = 37100             # meter status (uint16)
_METER_COUNT = 15                # ... through 37114
_METER_POWER_OFFSET = 13         # 37113 — meter active power (int32, W)

# Register 37100: 0 = offline, 1 = normal.
_METER_STATUS_NORMAL = 1

# ``as_of`` is stamped to the second, unlike the cloud's 5-minute bucket. The
# boost coordinator compares it for equality to refuse acting twice on one
# reading (issue #562); at this resolution every read is genuinely new data, so
# the guard stops firing — correctly, because a 1-second-old local reading
# *does* already contain the load change the cloud bucket could not.
_AS_OF_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class ModbusConfig:
    """Runtime Modbus settings loaded from ``.env``."""

    mac: Optional[str]
    host: Optional[str]
    port: int = _DEFAULT_PORT
    unit_id: int = _DEFAULT_UNIT_ID
    timeout_s: float = _DEFAULT_TIMEOUT_S
    cache_ttl_s: int = _DEFAULT_CACHE_TTL_S

    @property
    def enabled(self) -> bool:
        """False when neither an address nor a MAC is configured.

        Disabled is not an error — it is how a machine with no dongle (or CI,
        which must never touch the real inverter) stays on the cloud path.
        """
        return bool(self.host or self.mac)


# Serialises every read: the dongle allows exactly one client at a time, and a
# second session drops the first mid-read.
_read_lock: Optional[asyncio.Lock] = None

# The address actually in use, which may differ from the configured hint after a
# MAC rediscovery. Deliberately process-lifetime memory rather than a write-back
# to ``.env``: that file is the repo's secret store, and re-resolving a moved
# lease at startup costs one lookup.
_runtime_host: Optional[str] = None

# (monotonic timestamp, snapshot) — see :data:`_DEFAULT_CACHE_TTL_S`.
_cache: Optional[Tuple[float, EnergyState]] = None

# Monotonic deadline before which no read is attempted after a failure.
_failure_until: float = 0.0

# Last serving-source verdict actually logged, so a steady state stays quiet and
# only a genuine Modbus↔cloud transition writes a line.
_last_source: Optional[str] = None


def _get_lock() -> asyncio.Lock:
    """Return the module lock, created lazily on the running loop."""
    global _read_lock
    if _read_lock is None:
        _read_lock = asyncio.Lock()
    return _read_lock


def _env_number(name: str, default: float, minimum: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default


def _load_config() -> ModbusConfig:
    """Read Modbus settings from ``.env``.

    Missing settings are not an error: the client reports itself disabled and
    the caller falls back to the cloud, exactly as an unreachable dongle would.
    """
    load_dotenv(override=True)
    mac = (os.getenv("HUAWEI_MODBUS_MAC") or "").strip() or None
    host = (os.getenv("HUAWEI_MODBUS_HOST") or "").strip() or None
    port = int(_env_number("HUAWEI_MODBUS_PORT", _DEFAULT_PORT, 1))
    unit_id = int(_env_number("HUAWEI_MODBUS_UNIT_ID", _DEFAULT_UNIT_ID, 0))
    timeout_s = _env_number("HUAWEI_MODBUS_TIMEOUT_S", _DEFAULT_TIMEOUT_S, _MIN_TIMEOUT_S)
    cache_ttl_s = int(_env_number("HUAWEI_MODBUS_CACHE_TTL_S", _DEFAULT_CACHE_TTL_S, 1))
    return ModbusConfig(mac, host, port, unit_id, timeout_s, cache_ttl_s)


def _i32(registers: Sequence[int], offset: int) -> int:
    """Two consecutive holding registers as a signed big-endian int32."""
    return struct.unpack(
        ">i", struct.pack(">HH", registers[offset], registers[offset + 1])
    )[0]


def _read_blocks(config: ModbusConfig, host: str) -> Tuple[List[int], List[int]]:
    """Connect, read the two register blocks, close. Blocking — runs in a thread.

    Raises on any transport or protocol failure; the caller turns that into a
    cloud fallback rather than an exception.
    """
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(host, port=config.port, timeout=config.timeout_s)
    if not client.connect():
        raise ConnectionError(f"no Modbus TCP session to {host}:{config.port}")
    try:
        blocks: List[List[int]] = []
        for start, count in (
            (_INVERTER_START, _INVERTER_COUNT),
            (_METER_START, _METER_COUNT),
        ):
            reply = client.read_holding_registers(
                address=start, count=count, device_id=config.unit_id
            )
            if reply.isError():
                raise IOError(f"register block {start} read failed: {reply}")
            blocks.append(list(reply.registers))
        return blocks[0], blocks[1]
    finally:
        # Always hand the single client slot back, including on a failed read —
        # a leaked session locks every later attempt out of the dongle.
        client.close()


def _state_from_registers(
    inverter: Sequence[int], meter: Sequence[int]
) -> EnergyState:
    """Build an :class:`EnergyState` from one pair of register blocks."""
    state = EnergyState()

    state.pv_power_w = float(_i32(inverter, _ACTIVE_POWER_OFFSET))
    state.inverter_reachable = True

    meter_status = meter[0]
    if meter_status == _METER_STATUS_NORMAL:
        # The one and only place register 37113's sign is interpreted.
        # **Positive = exporting** — the opposite of the cloud's
        # ``meterActivePower``; see the module docstring for the proof.
        watts = float(_i32(meter, _METER_POWER_OFFSET))
        state.grid_export_w = round(max(watts, 0.0), 1)
        state.grid_import_w = round(max(-watts, 0.0), 1)
        state.meter_reachable = True
    else:
        logger.warning("⚠️ Power sensor reports status %s (not normal)", meter_status)

    # Reuses the cloud client's derivation so surplus and house consumption are
    # computed in one place for both sources.
    _derive(state)
    state.as_of = datetime.now().strftime(_AS_OF_FORMAT)
    return state


async def _resolve_host(config: ModbusConfig) -> Optional[str]:
    """The address to try, preferring one already known to work this process.

    Mirrors :func:`src.camera_client._recover_host`: the configured IP is a
    hint, the MAC is the truth. Returns ``None`` only when there is nothing to
    try at all.
    """
    return _runtime_host or config.host or await _rediscover(config)


async def _rediscover(config: ModbusConfig) -> Optional[str]:
    """Look the dongle up by MAC, best-effort. Never raises."""
    global _runtime_host

    if not config.mac:
        return None
    try:
        from src.network_client import resolve_ip_by_mac

        found = await resolve_ip_by_mac(config.mac)
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        logger.info("ℹ️ dongle MAC rediscovery failed: %s", exc)
        return None
    if not found:
        return None
    if found != _runtime_host:
        logger.info("ℹ️ dongle rediscovered at %s (MAC %s)", found, config.mac)
    _runtime_host = found
    return found


def _note_source(source: str, detail: str = "") -> None:
    """Log which source is serving, but only when the answer changes.

    A silent permanent fallback to the cloud is the failure this exists to make
    visible; a line per read at a 5 s poll would bury it instead.
    """
    global _last_source

    if source == _last_source:
        return
    _last_source = source
    if source == "modbus":
        logger.info("✅ Energy served by local Modbus%s", detail)
    else:
        logger.warning("⚠️ Energy falling back to the FusionSolar cloud%s", detail)


async def _attempt(config: ModbusConfig, host: str) -> EnergyState:
    """One bounded read cycle against ``host``."""
    inverter, meter = await asyncio.wait_for(
        asyncio.to_thread(_read_blocks, config, host), timeout=_READ_BUDGET_S
    )
    return _state_from_registers(inverter, meter)


async def fetch_modbus_state() -> Optional[EnergyState]:
    """Read the live energy flow off the dongle, or ``None`` if it can't be read.

    ``None`` is the whole error channel — unconfigured, unreachable, moved,
    rebooting, or answering nonsense all come back the same way, and the caller
    falls back to the cloud. Never raises.

    Only one read is ever in flight (the dongle tolerates a single client), and
    a successful snapshot is reused for ``cache_ttl_s``, which is what keeps the
    poll cadence off the device at ~5 s regardless of how many callers ask.
    """
    global _cache, _failure_until, _runtime_host

    config = _load_config()
    if not config.enabled:
        return None

    now = time.monotonic()
    cached = _cache
    if cached is not None and now - cached[0] < config.cache_ttl_s:
        return cached[1]

    async with _get_lock():
        # Re-check under the lock: a caller that queued behind a read in flight
        # should take its result rather than immediately opening a second
        # session against a device that permits exactly one.
        now = time.monotonic()
        cached = _cache
        if cached is not None and now - cached[0] < config.cache_ttl_s:
            return cached[1]
        if now < _failure_until:
            return None

        host = await _resolve_host(config)
        if host is None:
            _note_source("cloud", " (no dongle address or MAC configured)")
            _failure_until = now + _FAILURE_BACKOFF_S
            return None

        try:
            state = await _attempt(config, host)
        except Exception as first_exc:  # noqa: BLE001 — any transport/protocol error
            # The lease may simply have moved. Re-resolve by MAC and retry once
            # before conceding to the cloud; a dongle that answers at a new
            # address is not an outage.
            rediscovered = await _rediscover(config)
            if rediscovered is None or rediscovered == host:
                logger.info("ℹ️ Modbus read from %s failed: %s", host, first_exc)
                _note_source("cloud", f" ({first_exc})")
                _failure_until = time.monotonic() + _FAILURE_BACKOFF_S
                return None
            try:
                state = await _attempt(config, rediscovered)
            except Exception as retry_exc:  # noqa: BLE001
                logger.info(
                    "ℹ️ Modbus read failed at %s and at rediscovered %s: %s",
                    host, rediscovered, retry_exc,
                )
                _note_source("cloud", f" ({retry_exc})")
                _failure_until = time.monotonic() + _FAILURE_BACKOFF_S
                return None
            host = rediscovered

        _runtime_host = host
        _failure_until = 0.0
        _cache = (time.monotonic(), state)
        _note_source("modbus", f" ({host})")
        logger.debug(
            "Modbus %s: PV %s W, import %s W, export %s W, load %s W",
            host, state.pv_power_w, state.grid_import_w,
            state.grid_export_w, state.house_consumption_w,
        )
        return state


def reset_state() -> None:
    """Drop cache, backoff and rediscovered host. For tests and CLI reruns."""
    global _cache, _failure_until, _runtime_host, _last_source

    _cache = None
    _failure_until = 0.0
    _runtime_host = None
    _last_source = None
