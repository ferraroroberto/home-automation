"""
MELCloud Home client
=====================
Non-UI core: authenticate with **MELCloud Home** (the new Mitsubishi
Electric platform), read live unit state, and write control commands
(power / mode / target temperature / fan speed).

The unit migrated from classic MELCloud (``app.melcloud.com``, served by
``pymelcloud``) to MELCloud Home, which is a different API.  This module
wraps ``aiomelcloudhome`` — a pure-async HTTP client that performs the
PKCE login flow with no browser dependency.

Shared by both the CLI (``src/list_devices.py``) and the Streamlit
control UI (``app/app.py``) so auth + fetch + write logic lives in
exactly one place.  Every call authenticates fresh and tears the session
down — simple and stateless, which suits Streamlit reruns and a
proof-of-concept.  A future load-balancer should hold one long-lived
client and refresh the token rather than re-login per call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aiomelcloudhome import (
    ATAFanSpeed,
    ATAOperationMode,
    ATAUnit,
    ATAVaneHorizontal,
    ATAVaneVertical,
    MELCloudHome,
)
from dotenv import load_dotenv

logger = logging.getLogger("melcloud")

# Fan speeds offered for selection: "Auto" plus the unit's numbered speeds.
_FAN_SPEED_NAMES = [
    ATAFanSpeed.ONE,
    ATAFanSpeed.TWO,
    ATAFanSpeed.THREE,
    ATAFanSpeed.FOUR,
    ATAFanSpeed.FIVE,
]


class MelCloudConfigError(RuntimeError):
    """Raised when required MELCloud credentials are missing."""


class DeviceNotFoundError(RuntimeError):
    """Raised when a write targets a unit id that no longer exists."""


@dataclass
class DeviceInfo:
    """Flattened snapshot of a single MELCloud Home air-to-air unit.

    Copied out of the live ``aiomelcloudhome`` models so the client/
    session can be closed before the data is handed back.  The
    ``operation_modes`` / ``fan_speeds`` / ``temp_ranges`` fields drive
    the control widgets.
    """

    unit_id: str
    name: str
    building: str
    power: Optional[bool]
    operation_mode: Optional[str]
    room_temperature: Optional[float]
    set_temperature: Optional[float]
    fan_speed: Optional[str]
    operation_modes: List[str] = field(default_factory=list)
    fan_speeds: List[str] = field(default_factory=list)
    temp_step: float = 0.5
    # mode name -> (min, max) target-temperature range for that mode.
    temp_ranges: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    # Vane (air-direction) state. Directions are ``None`` when the unit
    # has no controllable vane; the ``*_options`` lists drive the
    # detail-modal selectors and ``has_vane_*`` gates whether to show them.
    vane_vertical: Optional[str] = None
    vane_horizontal: Optional[str] = None
    vane_vertical_options: List[str] = field(default_factory=list)
    vane_horizontal_options: List[str] = field(default_factory=list)
    has_vane_vertical: bool = False
    has_vane_horizontal: bool = False


def _load_credentials() -> tuple[str, str]:
    """Read MELCLOUD_EMAIL / MELCLOUD_PASSWORD from the environment (.env)."""
    load_dotenv()
    email = os.getenv("MELCLOUD_EMAIL")
    password = os.getenv("MELCLOUD_PASSWORD")
    if not email or not password:
        raise MelCloudConfigError(
            "Missing credentials. Copy .env.example to .env and set "
            "MELCLOUD_EMAIL and MELCLOUD_PASSWORD (your MELCloud Home login)."
        )
    return email, password


def _available_modes(unit: ATAUnit) -> List[str]:
    """Derive selectable operation modes from the unit's capabilities."""
    caps = unit.capabilities
    if caps is None:
        modes = [m.value for m in ATAOperationMode]
    else:
        modes = [ATAOperationMode.HEAT.value]  # heat is always supported
        if caps.has_cool_operation_mode:
            modes.append(ATAOperationMode.COOL.value)
        if caps.has_auto_operation_mode:
            modes.append(ATAOperationMode.AUTOMATIC.value)
        if caps.has_dry_operation_mode:
            modes.append(ATAOperationMode.DRY.value)
        if caps.has_fan_operation_mode:
            modes.append(ATAOperationMode.FAN.value)
    # Make sure the current mode is always selectable.
    if unit.operation_mode is not None and unit.operation_mode.value not in modes:
        modes.append(unit.operation_mode.value)
    return modes


def _available_fan_speeds(unit: ATAUnit) -> List[str]:
    """Derive selectable fan speeds: Auto + the unit's numbered speeds."""
    caps = unit.capabilities
    count = caps.number_of_fan_speeds if caps and caps.number_of_fan_speeds else 5
    speeds = [ATAFanSpeed.AUTO.value] + [s.value for s in _FAN_SPEED_NAMES[:count]]
    if unit.set_fan_speed is not None and unit.set_fan_speed.value not in speeds:
        speeds.append(unit.set_fan_speed.value)
    return speeds


def _vane_vertical_options(unit: ATAUnit) -> List[str]:
    """Selectable vertical vane directions (the full enum), with the
    current value always kept selectable."""
    options = [v.value for v in ATAVaneVertical]
    current = unit.vane_vertical_direction
    if current is not None and current.value not in options:
        options.append(current.value)
    return options


def _vane_horizontal_options(unit: ATAUnit) -> List[str]:
    """Selectable horizontal vane directions (the full enum), with the
    current value always kept selectable."""
    options = [v.value for v in ATAVaneHorizontal]
    current = unit.vane_horizontal_direction
    if current is not None and current.value not in options:
        options.append(current.value)
    return options


def _temp_ranges(unit: ATAUnit) -> Dict[str, Tuple[float, float]]:
    """Map each temperature-bearing mode to its (min, max) target range."""
    caps = unit.capabilities
    if caps is None:
        return {}
    ranges: Dict[str, Tuple[float, float]] = {}
    if caps.min_temp_heat is not None and caps.max_temp_heat is not None:
        ranges[ATAOperationMode.HEAT.value] = (caps.min_temp_heat, caps.max_temp_heat)
    if caps.min_temp_cool is not None and caps.max_temp_cool is not None:
        cool = (caps.min_temp_cool, caps.max_temp_cool)
        ranges[ATAOperationMode.COOL.value] = cool
        ranges[ATAOperationMode.DRY.value] = cool  # dry shares the cool range
    if caps.min_temp_auto is not None and caps.max_temp_auto is not None:
        ranges[ATAOperationMode.AUTOMATIC.value] = (caps.min_temp_auto, caps.max_temp_auto)
    return ranges


def _snapshot(unit: ATAUnit, building: str) -> DeviceInfo:
    """Copy the fields of interest out of a live ATA unit."""
    caps = unit.capabilities
    step = 0.5 if (caps and caps.has_half_degree_increments) else 1.0
    has_vert = bool(caps.has_vane_vertical) if caps else False
    has_horiz = bool(caps.has_vane_horizontal) if caps else False
    return DeviceInfo(
        unit_id=unit.id,
        name=unit.name,
        building=building,
        power=unit.power,
        operation_mode=unit.operation_mode.value if unit.operation_mode else None,
        room_temperature=unit.room_temperature,
        set_temperature=unit.set_temperature,
        fan_speed=unit.set_fan_speed.value if unit.set_fan_speed else None,
        operation_modes=_available_modes(unit),
        fan_speeds=_available_fan_speeds(unit),
        temp_step=step,
        temp_ranges=_temp_ranges(unit),
        vane_vertical=(
            unit.vane_vertical_direction.value
            if unit.vane_vertical_direction
            else None
        ),
        vane_horizontal=(
            unit.vane_horizontal_direction.value
            if unit.vane_horizontal_direction
            else None
        ),
        vane_vertical_options=_vane_vertical_options(unit) if has_vert else [],
        vane_horizontal_options=(
            _vane_horizontal_options(unit) if has_horiz else []
        ),
        has_vane_vertical=has_vert,
        has_vane_horizontal=has_horiz,
    )


async def fetch_devices() -> List[DeviceInfo]:
    """Authenticate and return a flattened snapshot of every ATA unit."""
    email, password = _load_credentials()

    devices: List[DeviceInfo] = []
    logger.info("ℹ️ Authenticating with MELCloud Home as %s", email)
    async with MELCloudHome(username=email, password=password) as client:
        context = await client.get_context()
        for building in context.buildings:
            for unit in building.air_to_air_units:
                devices.append(_snapshot(unit, building.name))

    logger.info("✅ Fetched %d unit(s)", len(devices))
    return devices


async def set_device_state(
    unit_id: str,
    *,
    power: Optional[bool] = None,
    operation_mode: Optional[str] = None,
    set_temperature: Optional[float] = None,
    fan_speed: Optional[str] = None,
    vane_vertical_direction: Optional[str] = None,
    vane_horizontal_direction: Optional[str] = None,
) -> DeviceInfo:
    """Write the supplied controls to one ATA unit and return its new state.

    Only the non-``None`` arguments are written.  Returns a fresh
    :class:`DeviceInfo` snapshot read back after the command.
    """
    email, password = _load_credentials()

    mode_enum = ATAOperationMode(operation_mode) if operation_mode else None
    fan_enum = ATAFanSpeed(fan_speed) if fan_speed else None
    vane_vert_enum = (
        ATAVaneVertical(vane_vertical_direction)
        if vane_vertical_direction
        else None
    )
    vane_horiz_enum = (
        ATAVaneHorizontal(vane_horizontal_direction)
        if vane_horizontal_direction
        else None
    )

    async with MELCloudHome(username=email, password=password) as client:
        logger.info(
            "ℹ️ Writing power=%s mode=%s temp=%s fan=%s vert=%s horiz=%s "
            "to unit %s",
            power, operation_mode, set_temperature, fan_speed,
            vane_vertical_direction, vane_horizontal_direction, unit_id,
        )
        await client.control_ata_unit(
            unit_id,
            power=power,
            operation_mode=mode_enum,
            set_temperature=set_temperature,
            set_fan_speed=fan_enum,
            vane_vertical_direction=vane_vert_enum,
            vane_horizontal_direction=vane_horiz_enum,
        )

        # Read back so the caller sees the applied state.
        context = await client.get_context()
        for building in context.buildings:
            for unit in building.air_to_air_units:
                if unit.id == unit_id:
                    logger.info("✅ Applied changes to '%s'", unit.name)
                    return _snapshot(unit, building.name)

    raise DeviceNotFoundError(f"No unit with id {unit_id}")
