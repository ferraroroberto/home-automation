"""Pin a device by IP/hostname **or** MAC, and resolve a MAC to its live address.

Config values that hardcode a LAN IP break silently whenever DHCP reassigns the
device — you get a connection to the *wrong device*, not an error. Reservations
fix that, but the router's static-binding table is capped, so they don't scale to
every device. Pinning by MAC removes the need for a reservation slot entirely.

The seam is deliberately shaped so **no existing config has to change**: a value
that looks like an IP or hostname is returned untouched with no network call at
all, so the common path costs nothing. Only a MAC-shaped value triggers a lookup.

Two behaviours make this safe to put in front of a live device call:

* **Short-TTL cache** — a per-request resolve must not drag a full network read
  behind it.
* **Last-known-good fallback** — if the inventory read fails, the previously
  resolved address is returned rather than raising. A resolver that hard-fails
  while the router is briefly unreachable would be strictly worse than the
  hardcoded IP it replaces.

Usage::

    host = await resolve_device_host(os.getenv("SMA_INVERTER_HOST"))

Callers that need a URL's host swapped (a MAC cannot sit in a URL host position —
its colons collide with the port separator) use :func:`resolve_url_host`.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# How long a resolved MAC→IP mapping is trusted before we look again. Short
# enough that a DHCP move is picked up within a couple of minutes, long enough
# that a burst of device calls resolves once rather than once each.
RESOLVE_TTL_S = 120.0

# A MAC is six hex pairs separated by ':' or '-'. Anchored so an IPv6 literal or
# a hostname containing hex can never be mistaken for one.
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")

# mac(normalised) -> (resolved_ip, monotonic_deadline). The IP is retained past
# its deadline on purpose: an expired entry is still the last-known-good answer
# if the next lookup fails.
_cache: dict[str, tuple[str, float]] = {}


class DeviceAddressError(RuntimeError):
    """A MAC-pinned device could not be resolved to an address."""


def is_mac(value: Optional[str]) -> bool:
    """True when *value* is a MAC address rather than an IP or hostname."""
    return bool(_MAC_RE.match((value or "").strip()))


def _key(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


def clear_cache() -> None:
    """Drop every cached resolution (tests, and a manual 'refresh now')."""
    _cache.clear()


async def resolve_device_host(configured: Optional[str]) -> Optional[str]:
    """Return a connectable host for *configured*.

    An IP or hostname is returned unchanged, with no network access. A MAC is
    resolved to that device's current IP via the live inventory, cached for
    :data:`RESOLVE_TTL_S`.

    Raises :class:`DeviceAddressError` when a MAC cannot be resolved and no
    previous answer is available — a clear failure is far better than silently
    connecting to whatever now holds a stale address. ``None``/blank passes
    through as ``None`` so "not configured" stays distinguishable from "broken".
    """
    value = (configured or "").strip()
    if not value:
        return None
    if not is_mac(value):
        return value  # literal IP/hostname — the fast path, no lookup

    key = _key(value)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached and now < cached[1]:
        return cached[0]

    ip = await _lookup(value)
    if ip:
        _cache[key] = (ip, now + RESOLVE_TTL_S)
        if cached and cached[0] != ip:
            logger.info("ℹ️ device %s moved to %s (was %s)", value, ip, cached[0])
        return ip

    if cached:
        # Stale but real. Keep using it and re-check on the next call rather
        # than failing a device read because one inventory fetch missed.
        logger.info(
            "ℹ️ could not resolve %s right now — reusing last known %s",
            value, cached[0],
        )
        return cached[0]

    raise DeviceAddressError(
        f"device pinned by MAC {value} is not in the network inventory — "
        "it may be offline, or the router/AP read may be unavailable"
    )


async def resolve_url_host(url: Optional[str], mac: Optional[str]) -> Optional[str]:
    """Return *url* with its host replaced by the address *mac* resolves to.

    A MAC cannot be written into a URL's host position — its colons are
    indistinguishable from the port separator — so a URL-shaped setting takes its
    MAC from a companion value instead. With no *mac*, *url* is returned
    unchanged. Any port, scheme, path and query are preserved.
    """
    if not url:
        return url
    if not (mac or "").strip():
        return url
    host = await resolve_device_host(mac)
    if not host:
        return url
    parts = urlsplit(url)
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    if parts.username:
        cred = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{cred}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_device_host_sync(configured: Optional[str]) -> Optional[str]:
    """Blocking :func:`resolve_device_host`, for synchronous CLI entry points.

    Only safe from code that is *not* already inside an event loop (the ops
    scripts). Called with a loop running it would deadlock, so that case returns
    the value unresolved rather than hanging — async callers must await
    :func:`resolve_device_host` directly.
    """
    value = (configured or "").strip()
    if not value or not is_mac(value):
        return value or None
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve_device_host(value))
    logger.warning(
        "⚠️ resolve_device_host_sync called from a running event loop — "
        "returning %s unresolved; await resolve_device_host instead", value,
    )
    return value


async def _lookup(mac: str) -> Optional[str]:
    """Current IP for *mac* from the live inventory, or None."""
    try:
        from src.network_client import resolve_ip_by_mac

        return await resolve_ip_by_mac(mac)
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.info("ℹ️ MAC→IP lookup for %s failed: %s", mac, exc)
        return None
