"""Local UPS status read over NUT or Windows USB-HID battery telemetry."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("ups")

# Hide the console window each subprocess would otherwise pop on Windows
# (these run on every Plugs/Home UPS poll). No-op off Windows. Mirrors the
# guarded pattern in network_host.py.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

_UNKNOWN_RUNTIME_MINUTES = {71582788, 4294967295}
# Portable NUT-for-Windows lives inside the repo under the gitignored
# ``_local/`` umbrella (co-located so deps aren't scattered; never committed).
_PORTABLE_NUT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "_local"
    / "nut"
    / "nut-2.8.5"
    / "NUT-for-Windows-x86_64-SNAPSHOT-2.8.5.4499-master"
    / "mingw64"
).resolve()
_PORTABLE_NUT_UPSC = _PORTABLE_NUT_ROOT / "bin" / "upsc.exe"
_PORTABLE_NUT_USBHID = _PORTABLE_NUT_ROOT / "bin" / "usbhid-ups.exe"


@dataclass(frozen=True)
class UpsState:
    """Flattened UPS status for the dashboard."""

    available: bool
    source: str
    name: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    serial: Optional[str] = None
    status: str = "unknown"
    mains_online: Optional[bool] = None
    battery_charge_pct: Optional[float] = None
    runtime_seconds: Optional[int] = None
    load_pct: Optional[float] = None
    load_w: Optional[float] = None
    input_voltage_v: Optional[float] = None
    output_voltage_v: Optional[float] = None
    battery_voltage_v: Optional[float] = None
    nominal_power_w: Optional[float] = None
    nominal_va: Optional[float] = None
    replace_battery: Optional[bool] = None
    alarms: Tuple[str, ...] = ()
    error: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def fetch_ups_state() -> UpsState:
    """Read the local USB UPS state, preferring NUT when configured/available."""
    try:
        nut = _read_nut()
        if nut.available:
            return nut
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ NUT UPS read unavailable: %s", exc)
    try:
        nut = _read_nut_direct()
        if nut.available:
            return nut
    except Exception as exc:  # noqa: BLE001
        logger.info("ℹ️ Direct NUT HID read unavailable: %s", exc)

    try:
        return _read_windows_battery()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Failed to read UPS status: %s", exc)
        return UpsState(
            available=False,
            source="none",
            error=str(exc),
            updated_at=_now(),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n


def _to_int(value: object) -> Optional[int]:
    n = _to_float(value)
    if n is None:
        return None
    return int(round(n))


def _clean_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_nut() -> UpsState:
    """Read NUT with ``upsc`` when installed.

    ``UPS_NUT_DEVICE`` may name a device such as ``pc-ups@127.0.0.1``. Without
    it, the local portable NUT install's ``pc-ups`` name is used. If NUT is
    absent or has no devices this raises so the Windows HID fallback can run.
    """
    upsc = _find_upsc()
    if not upsc:
        raise RuntimeError("upsc not found")

    device = os.getenv("UPS_NUT_DEVICE", "").strip() or "pc-ups@127.0.0.1"
    if not device:
        listed = subprocess.run(
            [upsc, "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        if listed.returncode != 0:
            raise RuntimeError((listed.stderr or listed.stdout or "upsc -l failed").strip())
        devices = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not devices:
            raise RuntimeError("upsc listed no UPS devices")
        device = devices[0]

    result = subprocess.run(
        [upsc, device],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "upsc failed").strip())

    values: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        values[key.strip()] = raw.strip()

    charge = _to_float(values.get("battery.charge"))
    runtime = _to_int(values.get("battery.runtime"))
    return _state_from_nut_values(
        source="nut",
        name=device,
        values=values,
        charge=charge,
        runtime=runtime,
    )


def _read_nut_direct() -> UpsState:
    driver = _find_usbhid_ups()
    if not driver:
        raise RuntimeError("usbhid-ups not found")
    env = os.environ.copy()
    if _PORTABLE_NUT_ROOT.exists():
        env["PATH"] = f"{_PORTABLE_NUT_ROOT / 'bin'};{_PORTABLE_NUT_ROOT / 'sbin'};" + env.get("PATH", "")
        env.setdefault("NUT_CONFPATH", str(_PORTABLE_NUT_ROOT / "etc"))
    env.setdefault(
        "NUT_STATEPATH",
        str((Path(__file__).resolve().parents[1] / "_local" / "nut" / "nut-2.8.5" / "state").resolve()),
    )
    result = subprocess.run(
        [
            driver,
            "-s",
            "pc-ups",
            "-x",
            "port=auto",
            "-x",
            "vendorid=051d",
            "-x",
            "winhid",
            "-x",
            "pollonly",
            "-x",
            "lowbatt=20",
            "-d",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
        env=env,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "usbhid-ups failed").strip())
    values: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        values[key.strip()] = raw.strip()
    if not values:
        raise RuntimeError("usbhid-ups returned no UPS variables")
    charge = _to_float(values.get("battery.charge"))
    runtime = _to_int(values.get("battery.runtime"))
    return _state_from_nut_values(
        source="nut_direct",
        name="pc-ups",
        values=values,
        charge=charge,
        runtime=runtime,
    )


def _state_from_nut_values(
    *,
    source: str,
    name: str,
    values: Dict[str, str],
    charge: Optional[float],
    runtime: Optional[int],
) -> UpsState:
    flags = set((values.get("ups.status") or "").split())
    mains_online = True if "OL" in flags else False if "OB" in flags else None
    raw_replace_battery = True if "RB" in flags else False if flags else None
    replace_battery = (
        False
        if raw_replace_battery
        and _is_contradictory_battery_alarm("replace battery", charge, runtime)
        else raw_replace_battery
    )
    status = _status_from_nut(flags, mains_online)
    alarms = _nut_alarms(values, flags, replace_battery, charge, runtime, mains_online)

    return UpsState(
        available=True,
        source=source,
        name=name,
        model=_clean_text(values.get("ups.model")),
        manufacturer=_clean_text(values.get("ups.mfr")),
        serial=_clean_text(values.get("ups.serial")),
        status=status,
        mains_online=mains_online,
        battery_charge_pct=charge,
        runtime_seconds=runtime,
        load_pct=_to_float(values.get("ups.load")),
        load_w=_load_w(values),
        input_voltage_v=_to_float(values.get("input.voltage")),
        output_voltage_v=_to_float(values.get("output.voltage")),
        battery_voltage_v=_to_float(values.get("battery.voltage")),
        nominal_power_w=_to_float(values.get("ups.realpower.nominal")),
        nominal_va=_to_float(values.get("ups.power.nominal")),
        replace_battery=replace_battery,
        alarms=alarms,
        updated_at=_now(),
    )


def _find_upsc() -> Optional[str]:
    found = shutil.which("upsc")
    if found:
        return found
    if _PORTABLE_NUT_UPSC.exists():
        return str(_PORTABLE_NUT_UPSC)
    return None


def _find_usbhid_ups() -> Optional[str]:
    found = shutil.which("usbhid-ups")
    if found:
        return found
    if _PORTABLE_NUT_USBHID.exists():
        return str(_PORTABLE_NUT_USBHID)
    return None


def _status_from_nut(flags: Iterable[str], mains_online: Optional[bool]) -> str:
    flags = set(flags)
    if "OB" in flags:
        return "on_battery"
    if "OL" in flags and "CHRG" in flags:
        return "charging"
    if "OL" in flags:
        return "online"
    if "LB" in flags:
        return "low_battery"
    return "online" if mains_online is True else "unknown"


def _load_w(values: Dict[str, str]) -> Optional[float]:
    direct = _to_float(values.get("ups.realpower"))
    if direct is not None:
        return direct
    pct = _to_float(values.get("ups.load"))
    nominal = _to_float(values.get("ups.realpower.nominal"))
    if pct is None or nominal is None:
        return None
    return round(nominal * pct / 100.0, 1)


def _nut_alarms(
    values: Dict[str, str],
    flags: set[str],
    replace_battery: Optional[bool],
    charge_pct: Optional[float],
    runtime_seconds: Optional[int],
    mains_online: Optional[bool],
) -> Tuple[str, ...]:
    raw_alarm = _clean_text(values.get("ups.alarm"))
    alarms = []
    if raw_alarm and not _is_contradictory_battery_alarm(raw_alarm, charge_pct, runtime_seconds):
        alarms.append(raw_alarm)
    if replace_battery and not _is_contradictory_battery_alarm("replace battery", charge_pct, runtime_seconds):
        alarms.append("Replace battery")
    if "LB" in flags and mains_online is False:
        alarms.append("Low battery")
    if charge_pct is not None and charge_pct <= 20:
        alarms.append("Charge below 20%")
    return tuple(dict.fromkeys(alarms))


def _is_contradictory_battery_alarm(
    alarm: str,
    charge_pct: Optional[float],
    runtime_seconds: Optional[int],
) -> bool:
    text = alarm.lower()
    if "battery" not in text and "replace" not in text:
        return False
    return (
        charge_pct is not None
        and charge_pct > 20
        and runtime_seconds is not None
        and runtime_seconds > 0
    )


def _read_windows_battery() -> UpsState:
    powershell = shutil.which("powershell.exe") or (
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = (
        "Get-CimInstance -ClassName Win32_Battery | "
        "Select-Object Name,DeviceID,Caption,Description,BatteryStatus,"
        "EstimatedChargeRemaining,EstimatedRunTime,DesignVoltage,Status | "
        "ConvertTo-Json -Depth 3"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Win32_Battery failed").strip())
    raw = result.stdout.strip()
    if not raw:
        return UpsState(
            available=False,
            source="windows_battery",
            error="No USB HID UPS battery was reported by Windows.",
            updated_at=_now(),
        )

    payload = json.loads(raw)
    items = payload if isinstance(payload, list) else [payload]
    batteries = [item for item in items if isinstance(item, dict)]
    if not batteries:
        return UpsState(
            available=False,
            source="windows_battery",
            error="No USB HID UPS battery was reported by Windows.",
            updated_at=_now(),
        )
    info = _choose_windows_battery(batteries)
    status_code = _to_int(info.get("BatteryStatus"))
    charge = _to_float(info.get("EstimatedChargeRemaining"))
    runtime = _runtime_seconds(info.get("EstimatedRunTime"))
    voltage = _to_float(info.get("DesignVoltage"))
    if voltage is not None and voltage > 1000:
        voltage = round(voltage / 1000.0, 2)
    mains_online = _mains_from_windows_status(status_code)

    return UpsState(
        available=True,
        source="windows_battery",
        name=_clean_text(info.get("Name") or info.get("Caption") or "USB UPS"),
        model=_clean_text(info.get("Description")),
        status=_status_from_windows(status_code),
        mains_online=mains_online,
        battery_charge_pct=charge,
        runtime_seconds=runtime,
        battery_voltage_v=voltage,
        alarms=_windows_alarms(status_code, charge),
        updated_at=_now(),
    )


def _choose_windows_battery(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    def score(item: Dict[str, Any]) -> int:
        text = " ".join(str(item.get(k) or "") for k in ("Name", "DeviceID", "Caption", "Description"))
        lowered = text.lower()
        return int("ups" in lowered) + int("uninterruptible" in lowered)

    return sorted(items, key=score, reverse=True)[0]


def _runtime_seconds(value: object) -> Optional[int]:
    minutes = _to_int(value)
    if minutes is None or minutes < 0 or minutes in _UNKNOWN_RUNTIME_MINUTES:
        return None
    return minutes * 60


def _mains_from_windows_status(status_code: Optional[int]) -> Optional[bool]:
    if status_code in {1, 4, 5}:
        return False
    if status_code in {2, 3, 6, 7, 8, 9, 11}:
        return True
    return None


def _status_from_windows(status_code: Optional[int]) -> str:
    return {
        1: "on_battery",
        2: "online",
        3: "full",
        4: "low_battery",
        5: "critical",
        6: "charging",
        7: "charging",
        8: "low_battery_charging",
        9: "critical_charging",
        10: "unknown",
        11: "partly_charged",
    }.get(status_code or 0, "unknown")


def _windows_alarms(
    status_code: Optional[int], charge_pct: Optional[float]
) -> Tuple[str, ...]:
    alarms = []
    if status_code in {4, 8}:
        alarms.append("Low battery")
    if status_code in {5, 9}:
        alarms.append("Critical battery")
    if charge_pct is not None and charge_pct <= 20:
        alarms.append("Charge below 20%")
    return tuple(dict.fromkeys(alarms))
