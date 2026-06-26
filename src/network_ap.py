r"""
NETGEAR R9000 access-point source (issue #197 split).
=====================================================
One of the two devices feeding :class:`NetworkState`: the NETGEAR R9000 read over
``pynetgear`` (SOAP on :80). Even in AP mode the R9000 reports the whole LAN
(wired + wireless), so it carries the attached-device inventory on its own, plus
AP identity/health and the proven ``reboot_access_point()`` control.

Credentials come from ``.env`` (loopback LAN, never committed)::

    NETWORK_AP_HOST / NETWORK_AP_USERNAME / NETWORK_AP_PASSWORD
    NETWORK_AP_MAC (optional) — stable MAC of the AP; enables auto-rediscovery

Extracted verbatim from ``network_client``; the orchestrator imports the fetch /
rediscovery / reboot surface from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv

from src.network_types import (
    AccessPointHealth,
    NetDevice,
    NetworkCommandError,
    NetworkConfigError,
    _normalise_mac,
    _require,
)

logger = logging.getLogger(__name__)

# NETGEAR get_info DeviceMode -> human label (best-effort; 1 == AP here).
_AP_DEVICE_MODE = {"0": "router", "1": "access_point", "2": "bridge", "3": "repeater"}

# In-memory runtime override for the AP host. None means "use NETWORK_AP_HOST".
# Updated to the last IP that successfully answered; survives within the process
# lifetime so a rediscovered address sticks without touching .env.
_ap_runtime_host: Optional[str] = None


def _ap_creds() -> tuple[str, str, str]:
    load_dotenv(override=True)
    return (
        _require("NETWORK_AP_HOST"),
        _require("NETWORK_AP_USERNAME"),
        _require("NETWORK_AP_PASSWORD"),
    )


def _ap_effective_host() -> str:
    """Return the runtime-discovered host if one is set, else the configured host."""
    global _ap_runtime_host
    return _ap_runtime_host or _require("NETWORK_AP_HOST")


def _ap_mac() -> Optional[str]:
    """Return the normalised NETWORK_AP_MAC if configured, else None."""
    load_dotenv(override=True)
    raw = (os.getenv("NETWORK_AP_MAC") or "").strip()
    return _normalise_mac(raw) if raw else None


def _rediscover_ap_host(mac: str, leases: list[dict]) -> Optional[str]:
    """Return the IP for *mac* found in *leases*, or None if not present."""
    target = _normalise_mac(mac)
    for lease in leases:
        if _normalise_mac(lease.get("mac")) == target:
            return lease.get("ip") or None
    return None


def _fetch_ap_sync(host_override: Optional[str] = None) -> tuple[AccessPointHealth, list[NetDevice]]:
    """Blocking pynetgear read: AP identity + the full attached-device list."""
    global _ap_runtime_host
    from pynetgear import Netgear

    _, user, pwd = _ap_creds()
    host = host_override or _ap_effective_host()
    # This R9000 serves the SOAP API on :80 (pynetgear defaults to :5000).
    ng = Netgear(password=pwd, host=host, user=user, port=80)

    info = ng.get_info() or {}
    raw = ng.get_attached_devices_2() or ng.get_attached_devices() or []
    if not info and not raw:
        return AccessPointHealth(reachable=False, error="login or SOAP read failed"), []

    devices: list[NetDevice] = []
    for d in raw:
        dd = d._asdict()
        signal = dd.get("signal")
        link = dd.get("link_rate")
        devices.append(
            NetDevice(
                mac=dd.get("mac"),
                ip=dd.get("ip"),
                name=None if dd.get("name") in ("n/a", "", None) else dd.get("name"),
                conn_type=dd.get("type"),
                signal=int(signal) if str(signal).isdigit() else None,
                link_rate=int(link) if str(link).isdigit() else None,
                ssid=dd.get("ssid") or None,
                source="ap",
            )
        )

    health = AccessPointHealth(
        reachable=True,
        model=info.get("ModelName"),
        firmware=info.get("Firmwareversion"),
        mode=_AP_DEVICE_MODE.get(str(info.get("DeviceMode")), info.get("DeviceMode")),
        device_count=len(devices),
    )
    # Cache the working host so subsequent requests skip the configured IP if
    # it was stale and this was a rediscovery probe with a different address.
    _ap_runtime_host = host
    return health, devices


async def fetch_access_point() -> tuple[AccessPointHealth, list[NetDevice]]:
    """Async wrapper around the blocking pynetgear read."""
    try:
        return await asyncio.to_thread(_fetch_ap_sync)
    except NetworkConfigError:
        raise
    except Exception as exc:
        logger.warning("⚠️ access-point read failed: %s", exc)
        return AccessPointHealth(reachable=False, error=str(exc)), []


def reboot_access_point() -> None:
    """Reboot the NETGEAR R9000 (proven working via pynetgear)."""
    from pynetgear import Netgear

    _, user, pwd = _ap_creds()
    host = _ap_effective_host()
    ng = Netgear(password=pwd, host=host, user=user, port=80)
    if not ng.reboot():
        raise NetworkCommandError("access point rejected the reboot command")
    logger.info("ℹ️ access-point reboot command accepted")
