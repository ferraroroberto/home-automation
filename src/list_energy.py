r"""
Show live SMA energy flow (CLI)
===============================
Proof-of-concept: confirm the SMA integration returns live energy data —
prefer Sunny Portal cloud energy balance when configured, otherwise fall back
to local Speedwire/ennexOS reads — before building solar load-balancing on top.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_energy       # Windows
    ./.venv/bin/python -m src.list_energy                 # POSIX

The local energy meter fallback is read over Speedwire (no credentials). The
inverter, if ``SMA_INVERTER_HOST`` is set in ``.env``, is read over its local
ennexOS API or Speedwire depending on config; it is asleep at night, which the
output flags rather than treating as an error.
"""

from __future__ import annotations

import asyncio
import logging

from src.sma_client import EnergyState, fetch_energy_state


def _fmt_w(value: float | None) -> str:
    return f"{value:.0f} W" if value is not None else "n/a"


def _fmt_kwh(value: float | None) -> str:
    return f"{value:,.1f} kWh" if value is not None else "n/a"


def _print_state(s: EnergyState) -> None:
    print(f"  Energy meter:       {'reachable' if s.meter_reachable else 'NOT reachable'}"
          + (f" (serial {s.meter_serial})" if s.meter_serial else ""))
    print(f"  Grid import:        {_fmt_w(s.grid_import_w)}")
    print(f"  Grid export:        {_fmt_w(s.grid_export_w)}")
    if s.inverter_reachable:
        print(f"  PV production:      {_fmt_w(s.pv_power_w)}")
    else:
        print("  PV production:      n/a (inverter asleep or unreachable)")
    print(f"  House consumption:  {_fmt_w(s.house_consumption_w)}")
    print(f"  PV surplus:         {_fmt_w(s.pv_surplus_w)}  (+ = exporting, − = importing)")
    print(f"  Total grid import:  {_fmt_kwh(s.grid_import_kwh)}")
    print(f"  Total grid export:  {_fmt_kwh(s.grid_export_kwh)}")


async def main() -> None:
    """Fetch the live SMA energy snapshot and print it."""
    state = await fetch_energy_state()

    if not state.meter_reachable and not state.inverter_reachable:
        print("No SMA devices reachable on this network.")
        return

    print("\nLive energy flow:\n")
    _print_state(state)
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
