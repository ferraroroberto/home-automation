r"""
Show live home-network state (CLI / probe)
==========================================
Gating probe for issue #125 - confirm we can read internet/WiFi/LAN health and
the attached-device inventory, named by MAC, before building the Network tab.
Read-only by default; pass ``--reboot-ap`` to actually reboot the access point.

Run from the project root with the venv interpreter::

    & .\.venv\Scripts\python.exe -m src.list_network                 # Windows
    & .\.venv\Scripts\python.exe -m src.list_network --speedtest     # + throughput
    & .\.venv\Scripts\python.exe -m src.list_network --reboot-ap      # reboot the AP
    ./.venv/bin/python -m src.list_network                            # POSIX
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from src.network_client import (
    NetworkConfigError,
    NetworkState,
    fetch_network_state,
    reboot_access_point,
)


def _print_state(state: NetworkState) -> None:
    net = state.internet
    print("\n=== Internet ===")
    online = "UP" if net.online else "DOWN"
    print(
        f"  status={online}  external={_ms(net.external_ms)}  "
        f"gateway={_ms(net.gateway_ms)}  loss={_pct(net.packet_loss_pct)}"
    )
    if net.download_mbps is not None or net.upload_mbps is not None:
        print(
            f"  speed: down={net.download_mbps} Mbps  up={net.upload_mbps} Mbps  "
            f"via {net.speedtest_server or '?'}"
        )

    ap = state.access_point
    print("\n=== Access point (NETGEAR) ===")
    if ap.reachable:
        print(
            f"  {ap.model or '?'}  fw={ap.firmware or '?'}  mode={ap.mode or '?'}  "
            f"devices={ap.device_count}"
        )
    else:
        print(f"  unreachable: {ap.error or 'no response'}")

    r = state.router
    print("\n=== Router (Vodafone ZXHN F6600P) ===")
    print(
        f"  reachable={_yn(r.reachable)}  logged_in={_yn(r.authenticated)}"
        + (f"  ({r.error})" if r.error else "")
    )

    print(f"\n=== Attached devices ({len(state.devices)}) ===")
    if not state.devices:
        print("  (none)")
    # Wireless first, weakest signal first - that's what needs attention.
    ordered = sorted(
        state.devices,
        key=lambda d: (not d.is_wireless, d.signal if d.signal is not None else 999),
    )
    for d in ordered:
        sig = f"{d.signal}%" if d.signal is not None else "-"
        name = d.name or "(unnamed)"
        ssid = f" ssid={d.ssid}" if d.ssid else ""
        print(
            f"  {d.mac or '??':17}  {d.ip or '-':15}  {d.conn_type or '-':7}  "
            f"signal={sig:>4}  {name}{ssid}"
        )

    print(f"\n=== Alerts ({len(state.alerts)}) ===")
    for a in state.alerts:
        print(f"  ⚠️  {a}")
    if not state.alerts:
        print("  ✅ none")


def _ms(value: float | None) -> str:
    return f"{value:.0f} ms" if value is not None else "-"


def _pct(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None else "-"


def _yn(value: bool) -> str:
    return "yes" if value else "no"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live home-network probe (issue #125)")
    parser.add_argument("--speedtest", action="store_true", help="run an Ookla speed test (~15 s)")
    parser.add_argument("--reboot-ap", action="store_true", help="reboot the access point and exit")
    args = parser.parse_args()

    if args.reboot_ap:
        print("Rebooting the access point...")
        await asyncio.to_thread(reboot_access_point)
        print("✅ reboot command accepted - the AP will drop for ~1-2 min.")
        return

    try:
        state = await fetch_network_state(include_speedtest=args.speedtest)
    except NetworkConfigError as exc:
        print(f"\nConfig error: {exc}")
        return
    _print_state(state)
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
