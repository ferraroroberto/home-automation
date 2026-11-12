"""Read-only Tesla Fleet API client.

Two reads, both plain HTTP GETs:

* :func:`read_vehicle` first asks for the vehicle's connectivity state
  (``online`` / ``asleep`` / ``offline``). That endpoint answers from Tesla's
  side without contacting the car, so it never wakes it.
* Only when the car is ``online`` does it fetch ``vehicle_data`` (battery,
  range, charging, odometer, gear, speed, and position for the home flag).

There is deliberately no wake-up and no command call: the client can read the
car and nothing else, and ``tests/devices/test_tesla_client.py`` holds it to
that. A sleeping car is a state, not an error.

Never raises: every failure becomes a :class:`TeslaReading` with
``state="error"`` and a backoff window, so a broken login or a Tesla outage
cannot kill the sampler or flood the API. Coordinates are reduced to an
``at_home`` flag here and dropped; they never leave this module and are never
logged. Every request is counted so the sampler can log the cost.

UI-free; used by ``app/webapp/tesla_sampler.py`` and ``src/list_tesla.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any, Dict, Optional

import aiohttp

from src._backoff import BackoffTracker
from src._env import _env_float
from src._numbers import optional_float
from src.devices.tesla_auth import (
    TeslaAppConfig,
    access_token,
    is_configured,
    load_app_config,
)
from src.location_config import load_location_config
from src.presence.presence_client import distance_m

logger = logging.getLogger("tesla")

_KM_PER_MILE = 1.609344
_TIMEOUT_S = 20
# Gears that mean the car is moving or about to; ``P`` and no gear are parked.
DRIVING_GEARS = frozenset({"D", "R", "N"})
# Charging states that mean energy is flowing into the battery.
CHARGING_STATES = frozenset({"Charging", "Starting"})
# A failed login or outage is retried after 1 min, doubling to at most 30 min:
# each attempt is a billed request, so a dead source must not be hammered.
_backoff = BackoffTracker(base_s=60.0, max_s=1800.0)
_lock = threading.Lock()
_vehicle_id: Optional[str] = None


class VehicleState(str, Enum):
    """The connectivity state a read reports for the car.

    ``ONLINE``, ``ASLEEP`` and ``OFFLINE`` are Tesla's own words; ``UNCONFIGURED``
    means there is no app or login and ``ERROR`` that the read failed. A word
    Tesla grows later passes through ``TeslaReading.state`` as plain text.
    """

    ONLINE = "online"
    ASLEEP = "asleep"
    OFFLINE = "offline"
    UNCONFIGURED = "unconfigured"
    ERROR = "error"


@dataclass(frozen=True)
class TeslaSample:
    """One read of the car, in metric units, with no coordinates."""

    ts: int
    battery_pct: Optional[float]
    range_km: Optional[float]
    charging_state: Optional[str]
    charger_power_kw: Optional[float]
    charge_energy_added_kwh: Optional[float]
    odometer_km: Optional[float]
    shift_state: Optional[str]
    speed_kmh: Optional[float]
    at_home: Optional[bool]

    @property
    def driving(self) -> bool:
        return (self.shift_state in DRIVING_GEARS) or bool(self.speed_kmh)

    @property
    def charging(self) -> bool:
        return self.charging_state in CHARGING_STATES

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeslaReading:
    """The outcome of one :func:`read_vehicle` call.

    ``state`` is ``online``/``asleep``/``offline`` from Tesla, ``unconfigured``
    when there is no app or login, or ``error`` (``error`` says why).
    """

    state: str
    sample: Optional[TeslaSample] = None
    error: Optional[str] = None

    @property
    def awake(self) -> bool:
        """Whether the car answered awake: the only state a data read is worth making in."""
        return self.state == VehicleState.ONLINE


class _RequestCounter:
    """Requests made today, so the sampler can log the billed volume."""

    def __init__(self) -> None:
        self.day = date.today()
        self.count = 0

    def bump(self) -> int:
        today = date.today()
        if today != self.day:
            self.day, self.count = today, 0
        self.count += 1
        return self.count


REQUESTS = _RequestCounter()


def _miles_to_km(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value) * _KM_PER_MILE, 2)
    except (TypeError, ValueError):
        return None


def _at_home(lat: Any, lon: Any) -> Optional[bool]:
    """Home/away from a position, or ``None`` when either side is unknown."""
    lat_f, lon_f = optional_float(lat), optional_float(lon)
    home = load_location_config()
    if lat_f is None or lon_f is None or home is None:
        return None
    radius = _env_float("TESLA_HOME_RADIUS_M", 150.0)
    return distance_m(lat_f, lon_f, home.lat, home.lon) <= radius


def parse_vehicle_data(payload: Dict[str, Any], ts: Optional[int] = None) -> TeslaSample:
    """Turn a ``vehicle_data`` response body into a metric, coordinate-free sample."""
    data = payload.get("response") or {}
    charge = data.get("charge_state") or {}
    drive = data.get("drive_state") or {}
    vehicle = data.get("vehicle_state") or {}
    speed_mph = optional_float(drive.get("speed"))
    return TeslaSample(
        ts=int(ts if ts is not None else time.time()),
        battery_pct=optional_float(charge.get("battery_level")),
        range_km=_miles_to_km(charge.get("battery_range")),
        charging_state=charge.get("charging_state"),
        charger_power_kw=optional_float(charge.get("charger_power")),
        charge_energy_added_kwh=optional_float(charge.get("charge_energy_added")),
        odometer_km=_miles_to_km(vehicle.get("odometer")),
        shift_state=drive.get("shift_state"),
        speed_kmh=round(speed_mph * _KM_PER_MILE, 1) if speed_mph is not None else None,
        at_home=_at_home(drive.get("latitude"), drive.get("longitude")),
    )


async def _get(session: aiohttp.ClientSession, config: TeslaAppConfig, path: str) -> aiohttp.ClientResponse:
    token = await access_token(config)
    REQUESTS.bump()
    return await session.get(
        f"{config.fleet_base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _resolve_vehicle_id(session: aiohttp.ClientSession, config: TeslaAppConfig) -> str:
    """The vehicle to read: ``TESLA_VIN`` when set, else the account's only car."""
    global _vehicle_id
    if _vehicle_id:
        return _vehicle_id
    resp = await _get(session, config, "/api/1/vehicles")
    async with resp:
        if resp.status != 200:
            raise RuntimeError(f"vehicle list returned HTTP {resp.status}")
        vehicles = (await resp.json(content_type=None)).get("response") or []
    wanted = (os.getenv("TESLA_VIN") or "").strip().upper()
    for vehicle in vehicles:
        if not wanted or str(vehicle.get("vin", "")).upper() == wanted:
            _vehicle_id = str(vehicle.get("id_s") or vehicle.get("id"))
            return _vehicle_id
    raise RuntimeError("no matching vehicle on the account")


async def _read(config: TeslaAppConfig, with_data: bool, known_awake: bool) -> TeslaReading:
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        vehicle_id = await _resolve_vehicle_id(session, config)
        if not (known_awake and with_data):
            resp = await _get(session, config, f"/api/1/vehicles/{vehicle_id}")
            async with resp:
                if resp.status != 200:
                    raise RuntimeError(f"vehicle state returned HTTP {resp.status}")
                body = await resp.json(content_type=None)
                state = str((body.get("response") or {}).get("state") or "unknown")
            reading = TeslaReading(state=state)
            if not reading.awake or not with_data:
                return reading
        resp = await _get(
            session, config,
            f"/api/1/vehicles/{vehicle_id}/vehicle_data"
            "?endpoints=charge_state%3Bdrive_state%3Bvehicle_state%3Blocation_data",
        )
        async with resp:
            # 408: the car is asleep (it fell asleep since the last read). A
            # sleeping car answers 408 without being woken, so this is a state.
            if resp.status == 408:
                return TeslaReading(state=VehicleState.ASLEEP.value)
            if resp.status != 200:
                raise RuntimeError(f"vehicle data returned HTTP {resp.status}")
            payload = await resp.json(content_type=None)
    return TeslaReading(state=VehicleState.ONLINE.value, sample=parse_vehicle_data(payload))


async def read_vehicle(*, with_data: bool = True, known_awake: bool = False) -> TeslaReading:
    """Read the car's state, and its data only when it is already awake.

    ``with_data=False`` checks the state alone, for the sampler's quiet window.
    ``known_awake=True`` (the car was driving or charging at the last read)
    skips the state check and reads data directly, halving the billed requests
    while active; a car that has since slept answers ``vehicle_data`` with 408
    and is reported asleep, still without being woken.
    """
    config = load_app_config()
    if not is_configured(config):
        return TeslaReading(state=VehicleState.UNCONFIGURED.value)
    with _lock:
        wait = _backoff.seconds_remaining()
    if wait is not None:
        return TeslaReading(state=VehicleState.ERROR.value, error=f"backing off ({int(wait)} s left)")
    try:
        reading = await _read(config, with_data, known_awake)
    except Exception as exc:  # noqa: BLE001 — a read never raises to the sampler
        with _lock:
            delay = _backoff.record_failure()
        logger.warning("⚠️ Tesla read failed (%s); retry in %d s", exc, int(delay))
        return TeslaReading(state=VehicleState.ERROR.value, error=str(exc))
    with _lock:
        _backoff.record_success()
    return reading
