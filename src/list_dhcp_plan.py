r"""
Show the ordered DHCP reservation plan (CLI)
=============================================
Issue #170 — reads the live attached-device inventory, classifies each device
into a category range from ``config/dhcp_plan.json``, folds in the router's
existing static bindings, and prints a copy-ready ``MAC · device · category ·
current IP → planned IP`` list grouped by category, with each row's apply status
(``reserved`` / ``create`` / ``change``).

Read-only by default. Pass ``--apply`` to **push the create/change rows to the
router** (issue #176) — a deliberate, confirmed write to the live gateway: it
prompts for ``yes`` first, then writes one binding at a time and reports a
per-row result. Already-reserved rows are never re-written.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_dhcp_plan            # Windows, show plan
    & .\.venv\Scripts\python.exe -m src.list_dhcp_plan --apply    # Windows, push it
    ./.venv/bin/python -m src.list_dhcp_plan                      # POSIX
"""

from __future__ import annotations

import asyncio
import logging
import sys

from src.dhcp_plan import (
    DhcpPlan,
    binding_name,
    build_plan,
    device_inputs_from_inventory,
    load_dhcp_plan_config,
)
from src.network_client import (
    NetworkCommandError,
    NetworkConfigError,
    apply_dhcp_bindings,
    fetch_dhcp_bindings,
    fetch_network_state,
)
from src.network_display_names import load_network_display_names


# How each row's apply status renders in the CLI list.
_STATUS_TAG = {
    "reserved": "✅ reserved",
    "create": "➕ create",
    "change": "♻️ change",
    "none": "",
}


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
            tag = "⚠️ randomised MAC" if a.randomized else _STATUS_TAG.get(a.status, "")
            flag = f"  {tag}" if tag else ""
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


def _pending_rows(plan: DhcpPlan) -> list[dict]:
    """The create/change rows the apply path would write, as ``{name, mac, ip}``."""
    return [
        {"name": binding_name(a.label, a.mac), "mac": a.mac, "ip": a.planned_ip}
        for cat in plan.categories
        for a in cat.assignments
        if a.status in ("create", "change") and a.planned_ip
    ]


async def _apply(plan: DhcpPlan) -> None:
    """Confirm-gated write of the create/change rows to the live router."""
    rows = _pending_rows(plan)
    if not rows:
        print("\nNothing to apply — every planned row is already reserved. ✅")
        return
    print(f"\nAbout to write {len(rows)} binding(s) to the router:")
    for r in rows:
        print(f"  {r['mac']:17} -> {r['ip']:15} {r['name']}")
    print("\n⚠️  This writes to the live gateway. Type 'yes' to proceed: ", end="")
    if input().strip().lower() != "yes":
        print("Aborted — no changes made.")
        return
    try:
        results = await apply_dhcp_bindings(rows)
    except (NetworkConfigError, NetworkCommandError) as exc:
        print(f"\nApply failed: {exc}")
        return
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n=== Apply result: {ok}/{len(results)} written ===")
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        tail = "" if r.get("ok") else f"  ({r.get('error')})"
        print(f"  {mark} {r['mac']:17} -> {r['ip']:15}{tail}")


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    do_apply = "--apply" in sys.argv[1:]

    try:
        state = await fetch_network_state()
    except NetworkConfigError as exc:
        print(f"\nConfig error: {exc}")
        return

    overrides = load_network_display_names()
    config = load_dhcp_plan_config()
    # Fold in the router's existing reservations so the plan shows what's already
    # applied; best-effort, so a binding-read failure still prints a usable plan.
    try:
        bindings = {
            b["mac"].strip().upper(): b["ip"]
            for b in await fetch_dhcp_bindings()
            if b.get("mac") and b.get("ip")
        }
    except (NetworkConfigError, NetworkCommandError) as exc:
        # None (not {}) → status "unknown" rather than "create everything".
        print(f"\n⚠️  Could not read existing bindings ({exc}); status will show as unknown.")
        bindings = None
    devices = device_inputs_from_inventory(state.devices, overrides)
    plan = build_plan(devices, config, bindings)
    _print_plan(plan)

    if do_apply:
        await _apply(plan)
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
