"""
Athom per-circuit energy client
===============================
Non-UI core: read the **per-circuit** power draw measured by Athom BL0906
energy monitors clamped onto individual breakers (issue #25).

Where ``src/huawei_client.py`` answers *how much* the house is importing or
exporting as a single whole-home figure, this answers *where the consumption is
going* — which is the measurement the eventual solar load-balancing automation
needs before it can shift one specific load (the heat pump) to match PV surplus.

Hardware
--------
Athom "Energy Monitor (6 Channels)" — an ESP32-C3 running stock **ESPHome**
firmware over a BL0906 metering front-end, with up to six passive CT clamps.
Local-only and open: no cloud account, no vendor API, replaceable firmware.

Discovery — zero-config, on purpose
-----------------------------------
Meters are found over **mDNS** (``_esphomelib._tcp.local.``) and filtered to
Athom energy monitors by their own advertised metadata::

    package_import_url = github://athom-tech/esp32-configs/athom-energy-monitor-x6.yaml
    project_name       = China Athom Technology.Athom Energy Monitor(6 Channels)
    mac                = aabbccddee01

That filter is what keeps the household's *other* ESPHome devices (the two Home
Assistant Voice PE satellites answer the same service type) out of the list.

Nothing has to be registered anywhere: clamp a new meter on, join it to Wi-Fi,
and it appears on the next discovery sweep with its own six channels. This is
deliberate — more meters are already wired and waiting, and needing a code
change or a config edit per meter would make the feature useless between them.
``ATHOM_METER_HOSTS`` in ``.env`` is the static escape hatch for a network where
mDNS is blocked, exactly as ``ELGATO_LIGHT_HOSTS`` is for the lights.

All six channels, always
------------------------
A channel with no clamp fitted is **not** hidden. The meter reports six, so six
are returned, every read — a clamp added next week starts showing a live figure
with no code change and nothing to reconfigure. The count comes from the device
itself (its advertised ``(6 Channels)`` / ``-x6.yaml``, else the highest
``power_N`` sensor it actually published), so a 3-channel model would report
three without special-casing.

``None`` means "not measured this read" and is never collapsed into ``0.0`` —
0 W is a real, common answer here (an idle circuit, or a channel with no clamp)
and must stay distinguishable from a failed read.

Reading — one SSE snapshot, not 21 polls
----------------------------------------
ESPHome's web server exposes both ``GET /sensor/<id>`` per sensor and an
``/events`` SSE stream that dumps **every** entity's state on connect. A
six-channel meter is 21+ sensors, so the per-sensor path would mean 21 requests
per poll against a mains-powered ESP32 on 2.4 GHz Wi-Fi. One SSE connection
returns the same data in a single round trip; the stream is dropped as soon as
the snapshot is complete.

Sign convention
---------------
The BL0906 reports **signed** per-channel power: a CT clamp fitted with its
arrow against the direction of flow reads negative for an ordinary load. That
is an installation detail, not data, so it is corrected in software rather than
by demanding someone reopen the consumer unit — see :mod:`src.circuit_prefs`,
whose per-channel ``invert`` flag is toggled from the card's own dialog. Raw and
corrected values are both returned (``power_raw_w`` / ``power_w``) so the
correction is always visible and reversible.

Config (from ``.env``, all optional):

* ``ATHOM_METER_HOSTS`` — comma-separated ``host[:port]`` fallback list used
  when mDNS cannot run or finds nothing
* ``ATHOM_DISCOVERY_TTL_S`` — how long a discovery sweep is reused (default 300)
* ``ATHOM_CACHE_TTL_S`` — how long one meter read is reused (default 5)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger("athom")

_SERVICE_TYPE = "_esphomelib._tcp.local."
_DEFAULT_PORT = 80

# An Athom energy monitor identifies itself in its mDNS TXT record. Matching on
# the upstream package URL is the most specific signal (it names the hardware
# family); the friendly project name is the fallback for a re-flashed device
# that kept the name but lost the package reference.
_ATHOM_PACKAGE_HINT = "athom-energy-monitor"
_ATHOM_PROJECT_HINT = "athom energy monitor"

# How long an mDNS browse takes. Long enough for every meter on a quiet 2.4 GHz
# network to answer, short enough that a cold read is not a visible stall.
_DISCOVERY_TIMEOUT_S = 3.0

# The retry (only ever run when the first browse found nothing) waits longer.
# Measured cold, one process per browse: 3 s found the meter 11/12 times, 6 s
# 12/12 — too small a sample to call 6 s *proven* better, but a wider window can
# only add chances on a protocol where answers arrive whenever they arrive, and
# misses here look bursty rather than independent, so simply repeating the same
# 3 s can land inside the same interference. The cost is paid only on the miss
# path and only once per discovery TTL, so the happy path stays fast.
_DISCOVERY_RETRY_TIMEOUT_S = 6.0

# Discovery is far more expensive than a read and its answer changes only when
# hardware is added, so it is reused across polls. Five minutes means a newly
# joined meter shows up on its own within one coffee break.
_DEFAULT_DISCOVERY_TTL_S = 300

# How long an *empty* sweep is trusted when meters were known a moment ago.
# Measured on this network: one cold 3 s browse missed the meter 1 time in 20
# (2.4 GHz multicast at -68 dBm drops packets), so "found nothing" is far more
# often a lost packet than a removed meter. Re-checking in 30 s rather than 300
# means a genuinely retired meter still disappears promptly.
_DISCOVERY_EMPTY_TTL_S = 30

# One meter read is shared by every caller for this long. The PWA polls the IoT
# tab every 15 s and the Home Assistant integration polls independently, so
# without this each meter would field several SSE connections per interval for
# data that only changes every few seconds.
_DEFAULT_CACHE_TTL_S = 5

# Ceiling on collecting one meter's opening SSE snapshot. The dump lands in well
# under a second on a healthy link; this only has to cover a bad one.
_SNAPSHOT_TIMEOUT_S = 6.0

# Bound the parallel meter reads, matching the Tuya router's reasoning: a wall of
# meters must not open a socket each at once.
_READ_CONCURRENCY = 6

# What a meter reports when nothing overrides it. Every Athom energy monitor
# sold to date is a 6-channel unit; the value is only reached when the device
# advertises no channel count *and* published no per-channel sensor.
_FALLBACK_CHANNELS = 6

# Channel count as the device advertises it: "(6 Channels)" in the project name,
# or the "-x6" suffix of the upstream package file name.
_CHANNELS_IN_PROJECT = re.compile(r"\((\d+)\s*channels?\)", re.IGNORECASE)
_CHANNELS_IN_PACKAGE = re.compile(r"-x(\d+)\b", re.IGNORECASE)

# ESPHome SSE ids are "<domain>-<object_id>"; the per-channel sensors are
# power_N / current_N / energy_N.
_CHANNEL_SENSOR = re.compile(r"^sensor-(power|current|energy)_(\d+)$")

# Device-wide sensors mapped onto MeterState fields.
_DEVICE_SENSORS = {
    "sensor-voltage": "voltage_v",
    "sensor-frequency": "frequency_hz",
    "sensor-temperature": "temperature_c",
    "sensor-wifi_signal_db": "wifi_rssi_dbm",
    "sensor-total_power": "total_power_w",
    "sensor-total_energy": "total_energy_kwh",
}


class AthomDiscoveryError(RuntimeError):
    """Raised when mDNS discovery cannot run at all (zeroconf missing)."""


@dataclass(frozen=True)
class MeterEndpoint:
    """One reachable Athom meter's LAN endpoint plus its advertised identity."""

    meter_id: str
    host: str
    port: int = _DEFAULT_PORT
    name: Optional[str] = None
    model: Optional[str] = None
    channel_count: int = _FALLBACK_CHANNELS

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class CircuitReading:
    """One CT-clamp channel of one meter.

    Always present for every channel the meter has, clamp fitted or not — see
    the module docstring. ``power_w`` is sign-corrected per
    :mod:`src.circuit_prefs`; ``power_raw_w`` is what the meter actually said.
    """

    channel: int
    #: Stable rename/prefs key, ``"<meter_id>:<channel>"``. Survives a DHCP move
    #: and a re-discovery because the meter id is its MAC, not its address.
    key: str
    power_w: Optional[float] = None
    power_raw_w: Optional[float] = None
    current_a: Optional[float] = None
    energy_kwh: Optional[float] = None
    inverted: bool = False


@dataclass
class MeterState:
    """Flattened snapshot of one meter and all of its channels."""

    meter_id: str
    name: str
    model: Optional[str] = None
    host: Optional[str] = None
    reachable: bool = False
    error: Optional[str] = None
    voltage_v: Optional[float] = None
    frequency_hz: Optional[float] = None
    temperature_c: Optional[float] = None
    wifi_rssi_dbm: Optional[float] = None
    total_power_w: Optional[float] = None
    total_energy_kwh: Optional[float] = None
    channels: List[CircuitReading] = field(default_factory=list)


@dataclass
class CircuitsState:
    """Everything ``GET /api/circuits`` needs, in one flattened snapshot.

    ``discovery_ok`` is a genuine third state, not folded into "no meters":
    *"mDNS could not run"* and *"mDNS ran and this home has no meters"* are
    different facts and the UI says different things about them.
    """

    meters: List[MeterState] = field(default_factory=list)
    discovery_ok: bool = False
    error: Optional[str] = None


# (monotonic deadline, endpoints) for the discovery sweep — see _discover().
_discovery_cache: Optional[tuple[float, List[MeterEndpoint]]] = None

# meter_id -> (monotonic deadline, state) for the per-meter read cache.
_state_cache: Dict[str, tuple[float, MeterState]] = {}


def _env_int(name: str, default: int) -> int:
    """Read a positive int from ``.env``, falling back with a warning."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("⚠️ Invalid %s=%s; using %s", name, raw, default)
        return default
    if value < 0:
        logger.warning("⚠️ %s must not be negative (%s); using %s", name, value, default)
        return default
    return value


def _normalise_mac(raw: str) -> str:
    """``aabbccddee01`` / ``88-56-a6-…`` → ``AA:BB:CC:DD:EE:01``."""
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", raw or "")
    if len(hex_only) != 12:
        return (raw or "").strip().upper()
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2)).upper()


def _channel_count(project_name: str, package_url: str) -> int:
    """Channels this model has, taken from its own advertised metadata."""
    match = _CHANNELS_IN_PROJECT.search(project_name or "")
    if match:
        return max(1, int(match.group(1)))
    match = _CHANNELS_IN_PACKAGE.search(package_url or "")
    if match:
        return max(1, int(match.group(1)))
    return _FALLBACK_CHANNELS


def _is_athom_meter(props: Dict[str, str]) -> bool:
    """True when an ESPHome mDNS record belongs to an Athom energy monitor."""
    package = (props.get("package_import_url") or "").lower()
    project = (props.get("project_name") or "").lower()
    return _ATHOM_PACKAGE_HINT in package or _ATHOM_PROJECT_HINT in project


def _parse_host(raw: str) -> Optional[MeterEndpoint]:
    """Parse one ``ATHOM_METER_HOSTS`` entry into an endpoint.

    A statically configured meter has no MAC until it is read, so its address
    stands in as the id. Discovery wins when both find the same device.
    """
    raw = (raw or "").strip()
    for prefix in ("http://", "https://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/", 1)[0].strip()
    if not raw:
        return None
    host, port = raw, _DEFAULT_PORT
    if raw.count(":") == 1:
        head, tail = raw.rsplit(":", 1)
        try:
            host, port = head.strip(), int(tail)
        except ValueError:
            host, port = raw, _DEFAULT_PORT
    if not host:
        return None
    return MeterEndpoint(meter_id=f"host:{host}", host=host, port=port, name=host)


def _configured_endpoints() -> List[MeterEndpoint]:
    """Static ``ATHOM_METER_HOSTS`` entries, for networks where mDNS is blocked."""
    raw = os.getenv("ATHOM_METER_HOSTS", "")
    parsed = [_parse_host(part) for part in raw.split(",")]
    return [endpoint for endpoint in parsed if endpoint is not None]


def _discover_sync(timeout_s: float) -> List[MeterEndpoint]:
    """Browse mDNS for Athom energy monitors (blocking; runs in a worker thread)."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as exc:  # pragma: no cover — requirements.txt pins it
        raise AthomDiscoveryError(
            "zeroconf is not installed; set ATHOM_METER_HOSTS or install requirements.txt"
        ) from exc

    class _Listener(ServiceListener):
        def __init__(self) -> None:
            self.endpoints: List[MeterEndpoint] = []

        def add_service(self, zc: "Zeroconf", service_type: str, name: str) -> None:
            info = zc.get_service_info(service_type, name)
            if info is None:
                return
            props = {
                (k.decode("utf-8", "ignore") if isinstance(k, bytes) else str(k)): (
                    v.decode("utf-8", "ignore") if isinstance(v, bytes) else ("" if v is None else str(v))
                )
                for k, v in (info.properties or {}).items()
            }
            if not _is_athom_meter(props):
                return
            # IPv4 only: the ESPHome web server is reached over v4 here, and a
            # link-local v6 address would need a scope the HTTP client can't use.
            addresses = [a for a in info.parsed_scoped_addresses() if ":" not in a]
            if not addresses:
                return
            mac = props.get("mac") or ""
            short = name.split(".", 1)[0]
            self.endpoints.append(
                MeterEndpoint(
                    meter_id=_normalise_mac(mac) if mac else f"host:{addresses[0]}",
                    host=addresses[0],
                    # The advertised port is the *native API* (6053); the web
                    # server this client reads is always plain HTTP on 80.
                    port=_DEFAULT_PORT,
                    name=props.get("friendly_name") or short,
                    model=props.get("project_name") or None,
                    channel_count=_channel_count(
                        props.get("project_name") or "",
                        props.get("package_import_url") or "",
                    ),
                )
            )

        def update_service(self, zc: "Zeroconf", service_type: str, name: str) -> None:
            self.add_service(zc, service_type, name)

        def remove_service(self, zc: "Zeroconf", service_type: str, name: str) -> None:
            return None

    listener = _Listener()
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, _SERVICE_TYPE, listener)
        time.sleep(timeout_s)
    finally:
        zc.close()

    unique: Dict[str, MeterEndpoint] = {}
    for endpoint in listener.endpoints:
        unique[endpoint.meter_id] = endpoint
    return list(unique.values())


async def discover_meters(force: bool = False) -> tuple[List[MeterEndpoint], Optional[str]]:
    """Return every known meter endpoint plus a discovery error, if any.

    The result is cached for ``ATHOM_DISCOVERY_TTL_S``.

    **A sweep never erases a known meter, empty or partial.** Measured on this
    network, one cold 3 s browse missed a given meter 1 time in 20 — 2.4 GHz
    multicast at -68 dBm loses packets, and mDNS reports that as "not found"
    rather than as a failure. With several meters on the network, that miss
    lands on a different subset each sweep, so a sweep finding *some* but not
    all previously-known meters is the common case, not the empty one. Letting
    either case silently replace a good list would make circuits blink out of
    the card at random, which is exactly the "a check that could not establish
    a fact reported it as a passing state" trap. So any meter missing from the
    current sweep — whether the sweep found nothing or found some other subset
    — is kept from the previous list and re-checked in
    :data:`_DISCOVERY_EMPTY_TTL_S` instead of the full TTL. A meter genuinely
    gone then shows up as unreachable on its own card — an honest answer —
    rather than vanishing.

    The single retry is belt-and-braces on top of that: a second browse costs
    nothing on the common path (it only runs when the first found nothing) and
    benefits from zeroconf's now-warm cache.
    """
    global _discovery_cache

    load_dotenv(override=True)
    ttl = _env_int("ATHOM_DISCOVERY_TTL_S", _DEFAULT_DISCOVERY_TTL_S)
    now = time.monotonic()

    cached = _discovery_cache
    if not force and cached is not None and now < cached[0]:
        return cached[1], None

    endpoints: Dict[str, MeterEndpoint] = {e.meter_id: e for e in _configured_endpoints()}
    error: Optional[str] = None
    swept: List[MeterEndpoint] = []
    for attempt in (1, 2):
        window = _DISCOVERY_TIMEOUT_S if attempt == 1 else _DISCOVERY_RETRY_TIMEOUT_S
        try:
            swept = await asyncio.to_thread(_discover_sync, window)
        except AthomDiscoveryError as exc:
            error = str(exc)
            logger.warning("⚠️ Athom discovery unavailable: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 — a browse failure must never be fatal
            error = f"mDNS discovery failed: {exc}"
            logger.warning("⚠️ Athom mDNS discovery failed: %s", exc)
            break
        if swept or attempt == 2:
            break
        logger.info(
            "ℹ️ Athom mDNS sweep found nothing; retrying once with a %ss window",
            _DISCOVERY_RETRY_TIMEOUT_S,
        )
    for endpoint in swept:
        endpoints[endpoint.meter_id] = endpoint

    previous = cached[1] if cached is not None else []
    missing = [e for e in previous if e.meter_id not in endpoints]
    if missing:
        # Could not prove these meters are gone — keep them (on top of whatever
        # this sweep did find) and look again soon.
        logger.info(
            "ℹ️ Athom sweep missed %d of %d known meter(s); keeping them and "
            "re-checking in %ds",
            len(missing), len(previous), _DISCOVERY_EMPTY_TTL_S,
        )
        for endpoint in missing:
            endpoints[endpoint.meter_id] = endpoint

    found = list(endpoints.values())
    if not found:
        # A sweep that found nothing at all is no more trustworthy on a cold
        # start (no previous discovery to fall back on yet) than a sweep that
        # missed some previously-known meters — same packet-loss reasoning
        # above. Caching it for the full TTL would leave a genuinely-present
        # meter invisible for minutes instead of the short retry window.
        _discovery_cache = (now + _DISCOVERY_EMPTY_TTL_S, found)
        return found, error

    _discovery_cache = (now + ttl, found)
    logger.info(
        "✅ Athom discovery: %s",
        ", ".join(f"{e.name} @ {e.host} ({e.channel_count}ch)" for e in found),
    )
    return found, error


def _as_float(value: object) -> Optional[float]:
    """Convert a sensor value to float, preserving "not measured" as None.

    ESPHome sends ``"nan"`` for a sensor that has not produced a reading yet;
    that is missing data, not zero, so it must not survive as a number.
    """
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN


def _parse_events(raw: str) -> Dict[str, Any]:
    """Collect ``id -> value`` from an ESPHome SSE snapshot.

    Only ``event: state`` frames carry entity state; ``ping`` (the device
    banner) and ``log`` (debug chatter) frames are skipped. Later frames win, so
    an update arriving mid-snapshot refines rather than corrupts the reading.
    """
    values: Dict[str, Any] = {}
    event = ""
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        payload: List[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                payload.append(line[len("data:") :].strip())
        if event != "state" or not payload:
            continue
        try:
            body = json.loads("\n".join(payload))
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict) and body.get("id"):
            values[str(body["id"])] = body.get("value")
    return values


async def _read_snapshot(
    session: aiohttp.ClientSession, endpoint: MeterEndpoint
) -> Dict[str, Any]:
    """Open the meter's SSE stream and return its opening state dump.

    ESPHome emits every entity's state immediately on connect and then streams
    live updates, so the snapshot is complete as soon as an id repeats. Reading
    to that point (rather than for a fixed wall-clock window) keeps a healthy
    meter fast without truncating a slow one.
    """
    chunks: List[str] = []
    seen: set[str] = set()
    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_S

    async with session.get(f"{endpoint.base_url}/events") as response:
        if response.status >= 400:
            raise aiohttp.ClientError(f"HTTP {response.status} from {endpoint.host}")
        async for line_bytes in response.content:
            chunks.append(line_bytes.decode("utf-8", "ignore"))
            if time.monotonic() > deadline:
                break
            line = line_bytes.decode("utf-8", "ignore").strip()
            if not line.startswith("data:") or '"id"' not in line:
                continue
            try:
                body = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            entity_id = str((body or {}).get("id") or "")
            if not entity_id:
                continue
            if entity_id in seen:
                break  # the dump has wrapped into live updates — snapshot done
            seen.add(entity_id)
    return _parse_events("".join(chunks))


def _build_state(
    endpoint: MeterEndpoint,
    values: Dict[str, Any],
    inverted: Dict[str, bool],
) -> MeterState:
    """Map one meter's SSE snapshot onto a :class:`MeterState`.

    Every channel the meter has is emitted whether or not it reported — a
    missing channel carries ``None`` (not measured), while a fitted clamp on an
    idle circuit legitimately carries ``0.0``.
    """
    state = MeterState(
        meter_id=endpoint.meter_id,
        name=endpoint.name or endpoint.host,
        model=endpoint.model,
        host=endpoint.host,
        reachable=True,
    )
    for sensor_id, attr in _DEVICE_SENSORS.items():
        setattr(state, attr, _as_float(values.get(sensor_id)))

    per_channel: Dict[int, Dict[str, Optional[float]]] = {}
    for sensor_id, raw in values.items():
        match = _CHANNEL_SENSOR.match(sensor_id)
        if not match:
            continue
        per_channel.setdefault(int(match.group(2)), {})[match.group(1)] = _as_float(raw)

    # The device's advertised count is the floor; a meter that published a
    # higher channel index than advertised is believed over its own metadata.
    count = max([endpoint.channel_count] + list(per_channel or {0: {}}))
    for channel in range(1, count + 1):
        readings = per_channel.get(channel, {})
        key = f"{endpoint.meter_id}:{channel}"
        raw_power = readings.get("power")
        invert = bool(inverted.get(key))
        state.channels.append(
            CircuitReading(
                channel=channel,
                key=key,
                power_raw_w=raw_power,
                power_w=(-raw_power if invert and raw_power is not None else raw_power),
                current_a=readings.get("current"),
                energy_kwh=readings.get("energy"),
                inverted=invert,
            )
        )
    return state


def _unreachable(endpoint: MeterEndpoint, reason: str) -> MeterState:
    """A meter that did not answer, still carrying all of its channels.

    The channel rows persist so a meter dropping off Wi-Fi dims its card rather
    than deleting circuits out from under whoever is watching them.
    """
    return MeterState(
        meter_id=endpoint.meter_id,
        name=endpoint.name or endpoint.host,
        model=endpoint.model,
        host=endpoint.host,
        reachable=False,
        error=reason,
        channels=[
            CircuitReading(channel=channel, key=f"{endpoint.meter_id}:{channel}")
            for channel in range(1, max(1, endpoint.channel_count) + 1)
        ],
    )


async def _read_meter(
    session: aiohttp.ClientSession,
    endpoint: MeterEndpoint,
    inverted: Dict[str, bool],
    cache_ttl_s: int,
) -> MeterState:
    """Read one meter, serving a recent snapshot when there is one."""
    now = time.monotonic()
    cached = _state_cache.get(endpoint.meter_id)
    if cached is not None and now < cached[0]:
        return cached[1]

    try:
        values = await _read_snapshot(session, endpoint)
    except asyncio.TimeoutError:
        logger.info("ℹ️ Athom meter %s timed out", endpoint.host)
        return _unreachable(endpoint, "Timed out — no answer on the LAN.")
    except aiohttp.ClientError as exc:
        logger.info("ℹ️ Athom meter %s unreachable: %s", endpoint.host, exc)
        return _unreachable(endpoint, "Offline — no response on the LAN (powered off?).")
    except Exception as exc:  # noqa: BLE001 — one bad meter must not sink the read
        logger.warning("⚠️ Athom meter %s read failed: %s", endpoint.host, exc)
        return _unreachable(endpoint, f"Read failed: {exc}")

    if not values:
        # Connected, answered, said nothing usable — distinct from unreachable.
        return _unreachable(endpoint, "Answered but published no sensor data.")

    state = _build_state(endpoint, values, inverted)
    _state_cache[endpoint.meter_id] = (now + cache_ttl_s, state)
    return state


async def fetch_circuits_state(force: bool = False) -> CircuitsState:
    """Read every discoverable Athom meter and return a flattened snapshot.

    Never raises. No meters, a meter that has dropped off Wi-Fi, and mDNS being
    unable to run are all normal results reported through ``discovery_ok`` /
    ``error`` / per-meter ``reachable`` rather than as exceptions — the same
    contract as :func:`src.huawei_client.fetch_energy_state`.

    ``force`` re-runs mDNS now instead of waiting out the discovery TTL (the
    explicit "Refresh" button), but still merges against the previous sweep —
    see :func:`discover_meters`. It must never be paired with wiping the
    discovery cache first: that would discard the very safety net a forced,
    possibly-partial sweep depends on.
    """
    load_dotenv(override=True)
    cache_ttl_s = _env_int("ATHOM_CACHE_TTL_S", _DEFAULT_CACHE_TTL_S)

    endpoints, discovery_error = await discover_meters(force=force)
    if not endpoints:
        return CircuitsState(meters=[], discovery_ok=discovery_error is None, error=discovery_error)

    from src.circuit_prefs import load_inverted_channels

    inverted = load_inverted_channels()
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=_SNAPSHOT_TIMEOUT_S + 2)

    async with aiohttp.ClientSession(timeout=timeout) as session:

        async def _bounded(endpoint: MeterEndpoint) -> MeterState:
            async with semaphore:
                return await _read_meter(session, endpoint, inverted, cache_ttl_s)

        meters = list(await asyncio.gather(*(_bounded(e) for e in endpoints)))

    meters.sort(key=lambda m: (m.name or "").lower())
    live = sum(1 for m in meters if m.reachable)
    logger.info(
        "✅ Athom circuits: %d/%d meter(s) reachable, %d channel(s)",
        live, len(meters), sum(len(m.channels) for m in meters),
    )
    return CircuitsState(
        meters=meters, discovery_ok=discovery_error is None, error=discovery_error
    )


def clear_read_cache() -> None:
    """Drop the per-meter read cache, keeping the discovered meter list.

    For anything that changes how a *known* meter's data is interpreted — a
    channel's sign flip is the only such thing today. Re-running discovery there
    would be both pointless and harmful: a sweep can legitimately come back
    empty (see :func:`discover_meters`), and with the cache just cleared there is
    no previous list to fall back on, so a flipped clamp could blank the whole
    card until the next sweep. Observed exactly that in testing.
    """
    _state_cache.clear()


def clear_caches() -> None:
    """Drop the discovery *and* read caches — an explicit "look again now".

    Only for the refresh action, where re-running discovery is the entire point
    (a meter joined a minute ago is invisible until the TTL lapses).
    """
    global _discovery_cache
    _discovery_cache = None
    _state_cache.clear()
