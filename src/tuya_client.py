"""
Tuya / Smart Life client
========================
Non-UI core for local Smart Life / Tuya device control.

Smart Life devices are Tuya devices.  TinyTuya needs Tuya Cloud only for
the one-time bootstrap that fetches local keys; runtime control and status
reads here are LAN-only through the gitignored ``devices.json`` file.

``devices.json`` is the TinyTuya wizard/snapshot output and may be either
a plain list of devices or snapshot format ``{"timestamp": ..., "devices": [...]}``.
It contains local keys, IPs, protocol versions, and per-device DPS mapping.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import tinytuya

logger = logging.getLogger("tuya")

_DEVICE_FILE = Path("devices.json")
_LOCAL_TIMEOUT_SECONDS = 1.0
_LOCAL_RETRY_LIMIT = 1

_SWITCH_CODES = ("switch_1", "switch", "switch_led")
_COVER_CONTROL_CODES = ("control", "control_back", "mach_operate")
_CURRENT_CODES = ("cur_current", "cur_current_1", "current")
_POWER_CODES = ("cur_power", "cur_power_1", "power")
_VOLTAGE_CODES = ("cur_voltage", "cur_voltage_1", "voltage")
_ENERGY_CODES = ("add_ele", "add_ele_1", "electricity", "energy", "total_energy")


class TuyaConfigError(RuntimeError):
    """Raised when required local Tuya metadata is missing."""


class TuyaDeviceNotFoundError(RuntimeError):
    """Raised when a requested Tuya device is not present in ``devices.json``."""


class TuyaCommandError(RuntimeError):
    """Raised when TinyTuya returns an error response or malformed payload."""


@dataclass(frozen=True)
class TuyaMapping:
    """One DPS mapping entry from ``devices.json``."""

    dps: str
    code: str
    type: Optional[str] = None
    scale: int = 0
    unit: Optional[str] = None


@dataclass(frozen=True)
class TuyaDeviceInfo:
    """Sanitized device summary safe to hand to UI/CLI callers."""

    device_id: str
    name: str
    category: Optional[str] = None
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    model: Optional[str] = None
    mac: Optional[str] = None
    uuid: Optional[str] = None
    sn: Optional[str] = None
    ip: Optional[str] = None
    version: float = 3.3
    has_valid_ip: bool = False
    has_local_key: bool = False
    switch_dps: Optional[str] = None
    cover_control_dps: Optional[str] = None
    energy_dps: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _LocalDevice:
    """Full local metadata used internally for LAN operations."""

    device_id: str
    name: str
    key: str
    address: str
    version: float
    raw: dict[str, Any]


def _load_device_entries() -> list[dict[str, Any]]:
    """Load TinyTuya devices from gitignored ``devices.json``."""
    if not _DEVICE_FILE.exists():
        raise TuyaConfigError(
            "Missing devices.json. Reuse the captured TinyTuya device file or "
            "run `python -m tinytuya wizard` once to fetch local keys."
        )

    with _DEVICE_FILE.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("devices", [])
    if not isinstance(payload, list) or not payload:
        raise TuyaConfigError("devices.json contains no Tuya devices")
    return [device for device in payload if isinstance(device, dict)]


def _has_valid_ip(device: dict[str, Any]) -> bool:
    """Return true for usable LAN IPv4 addresses."""
    ip = device.get("ip") or device.get("address") or ""
    if not isinstance(ip, str):
        return False
    ip = ip.strip()
    if not ip or ip == "Auto" or "No IP" in ip or "Error" in ip:
        return False
    parts = ip.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _version(value: object) -> float:
    """Normalize TinyTuya protocol versions from ``devices.json``."""
    if value is None or value == "":
        return 3.3
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("⚠️ Invalid Tuya protocol version %r; using 3.3", value)
        return 3.3


def _mapping(device: dict[str, Any]) -> dict[str, TuyaMapping]:
    """Return mapping entries keyed by Tuya code."""
    result: dict[str, TuyaMapping] = {}
    raw_mapping = device.get("mapping") or {}
    if not isinstance(raw_mapping, dict):
        return result

    for dps, entry in raw_mapping.items():
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if not code:
            continue
        values = entry.get("values") if isinstance(entry.get("values"), dict) else {}
        scale_raw = values.get("scale", 0)
        try:
            scale = int(scale_raw)
        except (TypeError, ValueError):
            scale = 0
        result[str(code)] = TuyaMapping(
            dps=str(dps),
            code=str(code),
            type=entry.get("type"),
            scale=scale,
            unit=values.get("unit"),
        )
    return result


def _snapshot_dps(device: dict[str, Any]) -> dict[str, Any]:
    """Return DPS values captured by TinyTuya scan/snapshot rows."""
    dps = device.get("dps")
    if isinstance(dps, dict) and isinstance(dps.get("dps"), dict):
        dps = dps["dps"]
    if not isinstance(dps, dict):
        return {}
    return {str(key): value for key, value in dps.items()}


def _fallback_switch_mapping(device: dict[str, Any]) -> Optional[TuyaMapping]:
    """Infer switch DPS from captured status when no mapping block exists."""
    dps = _snapshot_dps(device)
    if isinstance(dps.get("1"), bool):
        return TuyaMapping(dps="1", code="switch_1", type="Boolean")
    if isinstance(dps.get("20"), bool):
        return TuyaMapping(dps="20", code="switch_led", type="Boolean")
    return None


def _fallback_energy_mappings(device: dict[str, Any]) -> dict[str, TuyaMapping]:
    """Infer common metered-plug DPS mappings from captured status rows."""
    dps = _snapshot_dps(device)
    if {"18", "19", "20"}.issubset(dps):
        mappings = {
            "current_ma": TuyaMapping(dps="18", code="cur_current", type="Integer"),
            "power_w": TuyaMapping(dps="19", code="cur_power", type="Integer", scale=1),
            "voltage_v": TuyaMapping(dps="20", code="cur_voltage", type="Integer", scale=1),
        }
        if "17" in dps:
            mappings["energy_kwh"] = TuyaMapping(
                dps="17",
                code="add_ele",
                type="Integer",
                scale=3,
            )
        return mappings
    if {"4", "5", "6"}.issubset(dps):
        return {
            "current_ma": TuyaMapping(dps="4", code="cur_current", type="Integer"),
            "power_w": TuyaMapping(dps="5", code="cur_power", type="Integer"),
            "voltage_v": TuyaMapping(dps="6", code="cur_voltage", type="Integer"),
        }
    return {}


def _first_mapping(device: dict[str, Any], codes: tuple[str, ...]) -> Optional[TuyaMapping]:
    """Find the first mapping entry whose code is in ``codes``."""
    mapping = _mapping(device)
    for code in codes:
        entry = mapping.get(code)
        if entry:
            return entry
    if codes == _SWITCH_CODES:
        return _fallback_switch_mapping(device)
    return None


def _energy_mappings(device: dict[str, Any]) -> dict[str, TuyaMapping]:
    """Find model-specific energy mappings from captured DPS metadata."""
    mappings: dict[str, TuyaMapping] = {}
    for name, codes in {
        "current_ma": _CURRENT_CODES,
        "power_w": _POWER_CODES,
        "voltage_v": _VOLTAGE_CODES,
        "energy_kwh": _ENERGY_CODES,
    }.items():
        entry = _first_mapping(device, codes)
        if entry:
            mappings[name] = entry
    return mappings or _fallback_energy_mappings(device)


def _sanitize(device: dict[str, Any]) -> TuyaDeviceInfo:
    """Return a non-secret summary for one device entry."""
    switch = _first_mapping(device, _SWITCH_CODES)
    cover = _first_mapping(device, _COVER_CONTROL_CODES)
    energy = _energy_mappings(device)
    return TuyaDeviceInfo(
        device_id=str(device.get("id") or device.get("dev_id") or ""),
        name=str(device.get("name") or device.get("id") or "Unnamed Tuya device"),
        category=device.get("category"),
        product_id=device.get("product_id") or device.get("productId"),
        product_name=device.get("product_name") or device.get("productName"),
        model=device.get("model"),
        mac=device.get("mac") or device.get("mac_address") or device.get("macAddress"),
        uuid=device.get("uuid"),
        sn=device.get("sn"),
        ip=device.get("ip") or device.get("address"),
        version=_version(device.get("version", device.get("ver"))),
        has_valid_ip=_has_valid_ip(device),
        has_local_key=bool(device.get("key") or device.get("local_key")),
        switch_dps=switch.dps if switch else None,
        cover_control_dps=cover.dps if cover else None,
        energy_dps={name: entry.dps for name, entry in energy.items()},
    )


def _select_device(device_id: str) -> dict[str, Any]:
    """Find a device by id, preferring duplicate entries with usable LAN IPs."""
    entries = _load_device_entries()
    matches = [
        device
        for device in entries
        if str(device.get("id") or device.get("dev_id") or "") == device_id
    ]
    if not matches:
        raise TuyaDeviceNotFoundError(f"No Tuya device with id {device_id} in devices.json")
    with_ip = [device for device in matches if _has_valid_ip(device)]
    return with_ip[0] if with_ip else matches[0]


def _local_metadata(device_id: str) -> _LocalDevice:
    """Resolve full local metadata for one device id."""
    device = _select_device(device_id)
    key = str(device.get("key") or device.get("local_key") or "")
    if not key:
        raise TuyaConfigError(f"Device {device_id} has no local key in devices.json")
    return _LocalDevice(
        device_id=str(device.get("id") or device.get("dev_id")),
        name=str(device.get("name") or device_id),
        key=key,
        address=(device.get("ip") or device.get("address") or "Auto"),
        version=_version(device.get("version", device.get("ver"))),
        raw=device,
    )


def _connect(device_id: str, cls: type[tinytuya.Device] = tinytuya.Device) -> tinytuya.Device:
    """Create a short-timeout TinyTuya local device connection."""
    metadata = _local_metadata(device_id)
    logger.info("ℹ️ Connecting locally to Tuya device '%s' (%s)", metadata.name, device_id)
    try:
        device = cls(metadata.device_id, metadata.address, metadata.key, version=metadata.version)
    except RuntimeError as exc:
        raise TuyaCommandError(
            f"Could not find Tuya device {device_id} on the LAN. Refresh devices.json "
            "with a TinyTuya snapshot/update while on the home network."
        ) from exc
    device.set_socketPersistent(False)
    device.set_socketTimeout(_LOCAL_TIMEOUT_SECONDS)
    device.set_socketRetryLimit(_LOCAL_RETRY_LIMIT)
    device.set_sendWait(1)
    return device


def _raise_for_tinytuya_error(response: Any, action: str) -> None:
    """Turn TinyTuya error dictionaries into exceptions."""
    if isinstance(response, dict):
        if response.get("Err"):
            raise TuyaCommandError(
                f"{action} failed: {response.get('Error', 'TinyTuya error')} "
                f"(Err {response['Err']})"
            )
        if "Error" in response and not response.get("dps"):
            raise TuyaCommandError(f"{action} failed: {response['Error']}")


def _status(device_id: str, cls: type[tinytuya.Device] = tinytuya.Device) -> dict[str, Any]:
    """Read local status and return its DPS payload wrapper."""
    device = _connect(device_id, cls)
    response = device.status()
    _raise_for_tinytuya_error(response, f"Read Tuya status {device_id}")
    if not isinstance(response, dict) or not isinstance(response.get("dps"), dict):
        raise TuyaCommandError(f"Read Tuya status {device_id} returned no DPS payload")
    return response


def _scaled(value: Any, mapping: TuyaMapping) -> Optional[float]:
    """Apply Tuya mapping scale where present while preserving missing data."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / (10 ** mapping.scale)


def list_devices() -> list[TuyaDeviceInfo]:
    """List Tuya devices captured in local ``devices.json``."""
    devices = [_sanitize(device) for device in _load_device_entries()]
    logger.info("✅ Loaded %d local Tuya device(s)", len(devices))
    return devices


def read_device_state(device_id: str) -> dict[str, Any]:
    """Read one device's live switch + energy state in a single LAN status read.

    Both the on/off switch and the metered-plug energy values come from the
    same DPS payload, so this issues one ``status()`` round-trip rather than a
    separate read per concern.  Returns ``reachable=True`` with whatever fields
    the device exposes; an offline/timed-out device surfaces as a
    :class:`TuyaCommandError` so the caller can mark just that card unavailable
    without failing the whole listing.
    """
    metadata = _local_metadata(device_id)
    switch = _first_mapping(metadata.raw, _SWITCH_CODES)
    energy = _energy_mappings(metadata.raw)

    status = _status(device_id, tinytuya.OutletDevice)
    dps = status["dps"]

    result: dict[str, Any] = {
        "device_id": device_id,
        "reachable": True,
        "switch_on": None,
        "power_w": None,
        "current_ma": None,
        "voltage_v": None,
        "energy_kwh": None,
    }
    if switch and switch.dps in dps:
        result["switch_on"] = bool(dps.get(switch.dps))
    for name, mapping in energy.items():
        result[name] = _scaled(dps.get(mapping.dps), mapping)
    logger.info("✅ Read Tuya state from %s", device_id)
    return result


def set_switch(device_id: str, on: bool) -> dict[str, Any]:
    """Turn a Tuya plug/light switch on or off via local LAN control."""
    metadata = _local_metadata(device_id)
    switch = _first_mapping(metadata.raw, _SWITCH_CODES)
    if not switch:
        raise TuyaCommandError(f"Device {device_id} has no switch DPS mapping")

    device = _connect(device_id)
    logger.info(
        "ℹ️ Setting Tuya switch %s DPS %s (%s) to %s",
        device_id,
        switch.dps,
        switch.code,
        "ON" if on else "OFF",
    )
    response = device.set_value(switch.dps, on)
    _raise_for_tinytuya_error(response, f"Set Tuya switch {device_id}")
    logger.info("✅ Set Tuya switch %s", device_id)
    return response if isinstance(response, dict) else {"response": response}


def set_cover(device_id: str, action: Literal["open", "close", "stop"]) -> dict[str, Any]:
    """Open, close, or stop a Tuya blind via local LAN control."""
    metadata = _local_metadata(device_id)
    control = _first_mapping(metadata.raw, _COVER_CONTROL_CODES)
    if not control:
        raise TuyaCommandError(f"Device {device_id} has no cover control DPS mapping")

    device = _connect(device_id, tinytuya.CoverDevice)
    logger.info("ℹ️ Sending Tuya cover action %s to %s", action, device_id)
    if action == "open":
        response = device.open_cover()
    elif action == "close":
        response = device.close_cover()
    else:
        response = device.stop_cover()
    _raise_for_tinytuya_error(response, f"Set Tuya cover {device_id} {action}")
    logger.info("✅ Sent Tuya cover action %s to %s", action, device_id)
    return response if isinstance(response, dict) else {"response": response}


def get_energy(device_id: str) -> dict[str, Any]:
    """Read model-specific smart-plug energy values via local LAN status.

    DPS indexes vary by model, so this uses the captured ``mapping`` block
    instead of assuming fixed 18/19/20 indexes.  The returned ``raw`` block
    includes the original DPS values and mapping scale for calibration.
    """
    metadata = _local_metadata(device_id)
    mappings = _energy_mappings(metadata.raw)
    if not mappings:
        raise TuyaCommandError(f"Device {device_id} has no energy DPS mapping")

    status = _status(device_id, tinytuya.OutletDevice)
    dps = status["dps"]
    result: dict[str, Any] = {
        "device_id": device_id,
        "raw": {},
    }
    for name, mapping in mappings.items():
        raw_value = dps.get(mapping.dps)
        result[name] = _scaled(raw_value, mapping)
        result["raw"][name] = {
            "dps": mapping.dps,
            "code": mapping.code,
            "value": raw_value,
            "scale": mapping.scale,
            "unit": mapping.unit,
        }
    logger.info("✅ Read Tuya energy from %s", device_id)
    return result
