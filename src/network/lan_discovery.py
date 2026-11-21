"""LAN-discovery helpers shared by the Elgato and Athom device clients.

Both read a ``host[:port]`` list from ``.env`` for networks where mDNS is
blocked, and otherwise browse one mDNS service type for a few seconds. The
parsing and the browse/cancel lifecycle live here once so the two cannot drift.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional


def parse_host_port(raw: str, default_port: int) -> Optional[tuple[str, int]]:
    """Split one configured ``[http(s)://]host[:port][/path]`` entry.

    ``None`` for a blank entry. A port that is not a number leaves the whole
    text as the host, on ``default_port``.
    """
    raw = (raw or "").strip()
    for prefix in ("http://", "https://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/", 1)[0].strip()
    if not raw:
        return None
    host, port = raw, default_port
    if raw.count(":") == 1:
        head, tail = raw.rsplit(":", 1)
        try:
            host, port = head.strip(), int(tail)
        except ValueError:
            host, port = raw, default_port
    return (host, port) if host else None


def browse_mdns(
    service_type: str,
    on_service: Callable[[Any, str, str], None],
    timeout_s: float,
) -> None:
    """Browse ``service_type`` for ``timeout_s`` seconds (blocking), calling
    ``on_service(zeroconf, service_type, name)`` for every add or update.

    Raises ``ImportError`` when ``zeroconf`` is not installed; each caller
    re-raises it as its own discovery error with its own ``.env`` hint.
    """
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            on_service(zc, type_, name)

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            on_service(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            return None

    zc = Zeroconf()
    browser = None
    try:
        browser = ServiceBrowser(zc, service_type, _Listener())
        time.sleep(timeout_s)
    finally:
        # ``Zeroconf.close()`` leaves the browser's worker thread parked on its
        # queue for the life of the process; only ``cancel()`` ends it.
        if browser is not None:
            browser.cancel()
        zc.close()
