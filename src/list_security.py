r"""
Show live RISCO alarm state + event log (CLI / probe)
=====================================================
Proof-of-concept and the gating probe for issue #43: confirm the RISCO Cloud
integration logs in and returns usable data before building the Security tab on
top. Read-only - it never arms or disarms.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_security       # Windows
    ./.venv/bin/python -m src.list_security                 # POSIX

It deliberately prints more than the eventual UI needs, to answer three
panel-specific unknowns:

  1. **Partial/perimeter labels** - set the alarm to *Partial* or *Perimeter*
     in the native RISCO app, rerun this, and compare each partition's flags +
     armed groups against the disarmed baseline. Optional group letters can be
     put in RISCO_PARTIAL_GROUP / RISCO_PERIMETER_GROUP to label the state.
  2. **"Who did it"** - whether each event carries a usable ``user_id`` (the
     actor) or only a timestamp + description.
  3. **Cloud-tier access** - whether login succeeds at all (a paid-plan gate;
     RISCO also periodically blocks third-party clients).
"""

from __future__ import annotations

import asyncio
import logging

from src.risco_client import (
    RiscoCommandError,
    RiscoConfigError,
    SecurityEvent,
    SecurityState,
    fetch_events,
    fetch_security_state,
)


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _print_state(state: SecurityState) -> None:
    print(f"\nSystem state: {state.label}")
    print(
        "Top-level status: "
        f"systemStatus={state.system_status} "
        f"systemReady={state.system_ready} "
        f"trouble={state.trouble} "
        f"alarmPending={state.alarm_pending} "
        f"ongoingAlarm={state.ongoing_alarm} "
        f"memoryAlarm={state.memory_alarm} "
        f"assumedControlPanelState={state.assumed_control_panel_state}"
    )
    print(f"Perimeter supported: {_yn(state.perimeter_supported)}")
    print(f"Supported actions: {', '.join(state.supported_actions) or '(none)'}")

    print(f"\nPartitions ({len(state.partitions)}):")
    if not state.partitions:
        print("  (none)")
    for p in state.partitions:
        groups = ", ".join(p.armed_groups) if p.armed_groups else "-"
        print(
            f"  [id {p.id}] armed={_yn(p.armed)} partial={_yn(p.partially_armed)} "
            f"disarmed={_yn(p.disarmed)} arming={_yn(p.arming)} "
            f"triggered={_yn(p.triggered)} armed_groups={groups}"
        )

    print(f"\nDetectors / zones ({len(state.zones)}):")
    if not state.zones:
        print("  (none)")
    for z in state.zones:
        flags = []
        if z.triggered:
            flags.append("TRIGGERED")
        if z.bypassed:
            flags.append("BYPASSED")
        suffix = ("  " + " ".join(flags)) if flags else ""
        print(f"  [id {z.id}] {z.name} (type {z.type}){suffix}")


def _print_events(events: list[SecurityEvent]) -> None:
    print(f"\nRecent events ({len(events)}) - newest first:")
    if not events:
        print("  (none)")
        return
    # The actor question: do entries carry a user_id we can show as "who did it"?
    with_actor = sum(1 for e in events if e.user_id not in (None, "", 0))
    for e in events[:20]:
        actor = e.user_id if e.user_id not in (None, "",) else "-"
        label = e.name or e.type or e.category or "event"
        print(f"  {e.time}  user={actor}  {label}")
        if e.text:
            print(f"      {e.text}")
    print(f"\n  -> {with_actor}/{len(events)} events carry a user_id (actor).")


async def main() -> None:
    """Fetch the live RISCO snapshot + recent events and print them."""
    try:
        state = await fetch_security_state()
        events = await fetch_events(count=50)
    except RiscoConfigError as exc:
        print(f"\nConfig error: {exc}")
        return
    except RiscoCommandError as exc:
        print(f"\nRISCO Cloud error: {exc}")
        return

    _print_state(state)
    _print_events(events)
    print("\nNative WebUI command ids are used for Arm, Partial, and Perimeter.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
