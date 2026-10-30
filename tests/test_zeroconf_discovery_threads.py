"""mDNS discovery must not leave a worker thread behind per sweep.

A real ``zeroconf.ServiceBrowser`` is a thread parked on a queue; ``Zeroconf.close()``
does not end it, only ``ServiceBrowser.cancel()`` does. The webapp sweeps on every
telemetry tick, so one leaked thread per sweep grows without bound. The stand-ins
below mimic that contract; nothing here touches the network.
"""

from __future__ import annotations

import queue
import sys
import threading
import types

import pytest

from src.devices import elgato_client
from src.energy import athom_client

_BROWSER_THREAD = "fake-service-browser"


def _install_fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    class Zeroconf:
        def close(self) -> None:
            # Like the real one: shuts the sockets, leaves browser threads alone.
            return None

    class ServiceBrowser(threading.Thread):
        def __init__(self, zc: Zeroconf, service_type: str, listener: object) -> None:
            super().__init__(name=_BROWSER_THREAD, daemon=True)
            self._queue: "queue.Queue[None]" = queue.Queue()
            self.start()

        def run(self) -> None:
            self._queue.get()

        def cancel(self) -> None:
            self._queue.put(None)
            self.join()

    module = types.ModuleType("zeroconf")
    module.Zeroconf = Zeroconf
    module.ServiceBrowser = ServiceBrowser
    module.ServiceListener = object
    monkeypatch.setitem(sys.modules, "zeroconf", module)


def _browser_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _BROWSER_THREAD]


@pytest.mark.parametrize(
    "discover",
    [elgato_client._discover_sync, athom_client._discover_sync],
    ids=["elgato", "athom"],
)
def test_discovery_sweeps_leave_no_browser_thread(
    monkeypatch: pytest.MonkeyPatch, discover
) -> None:
    _install_fake_zeroconf(monkeypatch)

    for _ in range(5):
        discover(0)

    assert _browser_threads() == []
