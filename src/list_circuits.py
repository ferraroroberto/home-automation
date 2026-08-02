r"""
Show live per-circuit power (CLI)
=================================
Smoke test for the Athom CT-clamp meters: confirm every meter is discovered and
every channel reads before relying on it from the webapp or Home Assistant.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_circuits       # Windows
    ./.venv/bin/python -m src.list_circuits                 # POSIX

Meters are found over mDNS with no configuration — see
:mod:`src.athom_client`. Every channel the meter has is listed whether or not a
clamp is fitted, so an unclamped channel shows ``0 W`` rather than vanishing;
``n/a`` means the read did not produce a value at all.
"""

from __future__ import annotations

import asyncio
import logging

from src.athom_client import CircuitsState, MeterState, fetch_circuits_state
from src.circuit_prefs import load_circuit_display_names


def _fmt_w(value: float | None) -> str:
    return f"{value:,.0f} W" if value is not None else "n/a"


def _fmt(value: float | None, unit: str, digits: int = 1) -> str:
    return f"{value:,.{digits}f} {unit}" if value is not None else "n/a"


def _print_meter(meter: MeterState, names: dict[str, str]) -> None:
    print(f"\n{meter.name}  [{meter.meter_id}]")
    print(f"  Host:               {meter.host or 'n/a'}")
    if meter.model:
        print(f"  Model:              {meter.model}")
    if not meter.reachable:
        print(f"  Status:             NOT reachable — {meter.error or 'unknown reason'}")
        print(f"  Channels:           {len(meter.channels)} (no live data)")
        return

    print(f"  Status:             reachable")
    print(f"  Mains:              {_fmt(meter.voltage_v, 'V')} · {_fmt(meter.frequency_hz, 'Hz', 2)}")
    print(f"  Wi-Fi / temp:       {_fmt(meter.wifi_rssi_dbm, 'dBm', 0)} · {_fmt(meter.temperature_c, '°C')}")
    print(f"  Total:              {_fmt_w(meter.total_power_w)}  ({_fmt(meter.total_energy_kwh, 'kWh', 3)})")
    print("  Channels:")
    for reading in meter.channels:
        label = names.get(reading.key) or f"Clamp {reading.channel}"
        flipped = "  (sign flipped)" if reading.inverted else ""
        print(
            f"    {reading.channel}. {label:<32} "
            f"{_fmt_w(reading.power_w):>10}  "
            f"{_fmt(reading.current_a, 'A', 3):>10}  "
            f"{_fmt(reading.energy_kwh, 'kWh', 3):>12}{flipped}"
        )


def _print_state(state: CircuitsState) -> None:
    for meter in state.meters:
        _print_meter(meter, load_circuit_display_names())
    live = sum(1 for m in state.meters if m.reachable)
    channels = sum(len(m.channels) for m in state.meters)
    print(f"\n{live}/{len(state.meters)} meter(s) reachable · {channels} channel(s)\n")


async def main() -> None:
    """Discover the Athom meters, read them, and print every channel."""
    state = await fetch_circuits_state()

    if state.error:
        print(f"\nDiscovery problem: {state.error}")
    if not state.meters:
        print(
            "\nNo Athom energy monitors found.\n"
            "  • Check the meter is powered and joined to Wi-Fi.\n"
            "  • mDNS blocked on this network? Set ATHOM_METER_HOSTS=<ip> in .env.\n"
        )
        return

    print("\nLive per-circuit power:")
    _print_state(state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
