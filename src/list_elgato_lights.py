"""Show and optionally control Elgato lights from the LAN."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from src.elgato_client import (
    ElgatoCommandError,
    ElgatoConfigError,
    ElgatoDiscoveryError,
    ElgatoLight,
    fetch_lights,
    set_light_state,
)


def _print_light(light: ElgatoLight) -> None:
    print(f"{light.name} ({light.light_id})")
    if not light.reachable:
        print(f"  Unavailable: {light.error or 'unknown error'}")
        return
    print(f"  State:        {'ON' if light.on else 'OFF'}")
    print(f"  Brightness:   {light.brightness}%")
    if light.supports_temperature:
        print(f"  Temperature:  {light.temperature} mired ({light.temperature_k} K)")
    else:
        print("  Temperature:  not reported")
    if light.product_name:
        print(f"  Product:      {light.product_name}")
    if light.firmware:
        print(f"  Firmware:     {light.firmware}")


def _optional_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("--on must be true/false or on/off")


async def _main() -> int:
    parser = argparse.ArgumentParser(description="List/control Elgato lights")
    parser.add_argument("--id", dest="light_id", help="Light id from the list output")
    parser.add_argument("--on", type=_optional_bool, help="Set power: on/off")
    parser.add_argument("--brightness", type=int, help="Set brightness percent")
    parser.add_argument("--temperature", type=int, help="Set Elgato mired temperature")
    parser.add_argument("--kelvin", type=int, help="Set colour temperature in Kelvin")
    args = parser.parse_args()

    wants_write = (
        args.on is not None
        or args.brightness is not None
        or args.temperature is not None
        or args.kelvin is not None
    )
    try:
        if wants_write:
            if not args.light_id:
                parser.error("--id is required when setting a value")
            light = await set_light_state(
                args.light_id,
                on=args.on,
                brightness=args.brightness,
                temperature=args.temperature,
                temperature_k=args.kelvin,
            )
            _print_light(light)
            return 0

        lights = await fetch_lights()
    except (ElgatoConfigError, ElgatoDiscoveryError, ElgatoCommandError) as exc:
        print(f"Elgato lights unavailable: {exc}")
        return 2

    if not lights:
        print("No Elgato lights found.")
        return 1
    for idx, light in enumerate(lights):
        if idx:
            print()
        _print_light(light)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
