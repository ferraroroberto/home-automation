r"""
List MELCloud Home units (CLI)
==============================
Proof-of-concept: confirm the MELCloud Home API returns live data for the
connected Mitsubishi Electric units before building solar load-balancing
automation on top of it.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_devices       # Windows
    ./.venv/bin/python -m src.list_devices                 # POSIX

Reads MELCLOUD_EMAIL / MELCLOUD_PASSWORD from ``.env``.
"""

from __future__ import annotations

import asyncio
import logging

from src.melcloud_client import DeviceInfo, MelCloudConfigError, fetch_devices


def _fmt_temp(value: float | None) -> str:
    return f"{value:.1f} °C" if value is not None else "n/a"


def _fmt_power(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "ON" if value else "OFF"


def _print_device(device: DeviceInfo) -> None:
    print(f"  Name:               {device.name}")
    print(f"  Building:           {device.building}")
    print(f"  Room temperature:   {_fmt_temp(device.room_temperature)}")
    print(f"  Target temperature: {_fmt_temp(device.set_temperature)}")
    print(f"  Operation mode:     {device.operation_mode or 'n/a'}")
    print(f"  Fan speed:          {device.fan_speed or 'n/a'}")
    print(f"  Power state:        {_fmt_power(device.power)}")


async def main() -> None:
    """Fetch every MELCloud Home unit and print its live state."""
    devices = await fetch_devices()

    if not devices:
        print("No units found on this MELCloud Home account.")
        return

    print(f"\nFound {len(devices)} unit(s):\n")
    for index, device in enumerate(devices, start=1):
        print(f"Unit {index}:")
        _print_device(device)
        print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except MelCloudConfigError as exc:
        raise SystemExit(f"❌ {exc}")
