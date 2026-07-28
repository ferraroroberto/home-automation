r"""
Show live FusionSolar energy flow (CLI)
=======================================
Smoke test: confirm the Huawei FusionSolar integration returns live energy data
before building solar load-balancing on top of it.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_energy       # Windows
    ./.venv/bin/python -m src.list_energy                 # POSIX

The whole flow — PV production, house consumption and the grid exchange — comes
from the FusionSolar cloud in one call, using the ``FUSIONSOLAR_*`` credentials
in ``.env``. At night the inverter stops reporting PV, which the output flags
rather than treating as an error.
"""

from __future__ import annotations

import asyncio
import logging

from src.huawei_client import EnergyState, fetch_energy_state


def _fmt_w(value: float | None) -> str:
    return f"{value:.0f} W" if value is not None else "n/a"


def _fmt_kwh(value: float | None) -> str:
    return f"{value:,.1f} kWh" if value is not None else "n/a"


def _print_state(s: EnergyState) -> None:
    print(f"  Power sensor:       {'reachable' if s.meter_reachable else 'NOT reachable'}"
          + (f" (serial {s.meter_serial})" if s.meter_serial else ""))
    print(f"  Grid import:        {_fmt_w(s.grid_import_w)}")
    print(f"  Grid export:        {_fmt_w(s.grid_export_w)}")
    if s.inverter_reachable:
        print(f"  PV production:      {_fmt_w(s.pv_power_w)}")
    else:
        print("  PV production:      n/a (inverter asleep or unreachable)")
    print(f"  House consumption:  {_fmt_w(s.house_consumption_w)}")
    print(f"  PV surplus:         {_fmt_w(s.pv_surplus_w)}  (+ = exporting, − = importing)")
    print(f"  Grid import today:  {_fmt_kwh(s.grid_import_kwh)}")
    print(f"  Grid export today:  {_fmt_kwh(s.grid_export_kwh)}")


async def main() -> None:
    """Fetch the live FusionSolar energy snapshot and print it."""
    state = await fetch_energy_state()

    if not state.meter_reachable and not state.inverter_reachable:
        print("No FusionSolar data available (check FUSIONSOLAR_* in .env).")
        return

    print("\nLive energy flow:\n")
    _print_state(state)
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
