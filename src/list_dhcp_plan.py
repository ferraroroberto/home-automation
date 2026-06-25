r"""
Show the ordered DHCP reservation plan (CLI)
=============================================
Issue #170 — reads the live attached-device inventory, classifies each device
into a category range from ``config/dhcp_plan.json``, and prints a copy-ready
``MAC · device · category · current IP → planned IP`` list grouped by category.
**Read-only** — it never writes to the router; you apply the bindings by hand in
the F6600P's *DHCP Binding* form (automated write-back is phase 2).

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_dhcp_plan      # Windows
    ./.venv/bin/python -m src.list_dhcp_plan                 # POSIX
"""

from __future__ import annotations

import asyncio
import logging
import sys

from src.dhcp_plan import (
    DhcpPlan,
    build_plan,
    device_inputs_from_inventory,
    load_dhcp_plan_config,
)
from src.network_client import NetworkConfigError, fetch_network_state
from src.network_display_names import load_network_display_names


def _print_plan(plan: DhcpPlan) -> None:
    placed = 0
    for cat in plan.categories:
        print(f"\n=== {cat.label} ({cat.start}–{cat.end}) ===")
        if not cat.assignments:
            print("  (none)")
            continue
        for a in cat.assignments:
            planned = a.planned_ip or "—"
            current = a.current_ip or "—"
            arrow = "==" if a.planned_ip and a.planned_ip == a.current_ip else "->"
            flag = "  ⚠️ randomised MAC" if a.randomized else ""
            print(
                f"  {a.mac or '??':17}  {current:15} {arrow} {planned:15}  "
                f"{a.label}{flag}"
            )
            if a.planned_ip:
                placed += 1

    if plan.unassigned:
        print(f"\n=== Unassigned ({len(plan.unassigned)}) ===")
        for a in plan.unassigned:
            current = a.current_ip or "—"
            note = f"  (override → unknown '{a.category}')" if a.category else ""
            print(f"  {a.mac or '??':17}  {current:15}  {a.label}{note}")

    print(f"\n=== Warnings ({len(plan.warnings)}) ===")
    if not plan.warnings:
        print("  ✅ none")
    for w in plan.warnings:
        print(f"  ⚠️  {w}")

    print(f"\nPlanned reservations: {placed}")


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    try:
        state = await fetch_network_state()
    except NetworkConfigError as exc:
        print(f"\nConfig error: {exc}")
        return

    overrides = load_network_display_names()
    config = load_dhcp_plan_config()
    devices = device_inputs_from_inventory(state.devices, overrides)
    plan = build_plan(devices, config)
    _print_plan(plan)
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
