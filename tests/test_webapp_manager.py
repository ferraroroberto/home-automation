"""WebappManager startup-timeout handling (issue #687).

A cold boot-storm start has been observed taking the webapp process ~20
minutes to bind its port even though ``startup_timeout_seconds`` (15s) is
comfortably enough on a warm start. ``_wait_until_ready()`` must distinguish
"still alive, just slow" (``WebappStartupPending`` — worth continuing to
watch) from "already exited" (a real failure, plain ``RuntimeError``), and
``watch_until_resolved()`` is what the tray uses to keep tracking a pending
start on a background thread until it genuinely resolves either way.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.webapp import manager
from app.webapp.manager import WebappManager, WebappManagerConfig, WebappStartupPending

# What ``connect_ex`` returns for a loopback connect that finds no listener, per
# platform: the numbers differ, so only 0 may count as "in use". Hand-built from
# each OS's socket error table.
_CONNECT_EX_REFUSED = {"win32": 10061, "darwin": 61, "linux": 111}
_CONNECT_EX_TIMED_OUT = {"win32": 10035, "darwin": 35, "linux": 11}


class _FakeSocket:
    """A TCP socket that answers ``connect_ex`` from a listener table, never the network."""

    def __init__(self, listeners: set[tuple[str, int]], miss: int, calls: list) -> None:
        self._listeners = listeners
        self._miss = miss
        self._calls = calls

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def settimeout(self, seconds: float) -> None:
        self._calls.append(("settimeout", seconds))

    def connect_ex(self, address: tuple[str, int]) -> int:
        self._calls.append(("connect_ex", address))
        return 0 if address in self._listeners else self._miss


def _fake_sockets(monkeypatch, listeners: set[tuple[str, int]], miss: int) -> list:
    """Swap the manager's ``socket`` module for the fake; returns the call log."""
    calls: list = []

    def _open(family: int, kind: int) -> _FakeSocket:
        calls.append(("socket", family, kind))
        return _FakeSocket(listeners, miss, calls)

    monkeypatch.setattr(
        manager,
        "socket",
        SimpleNamespace(socket=_open, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM),
    )
    return calls


class _FakeProc:
    """Stand-in for the ``subprocess.Popen`` handle — ``poll()`` returns
    ``None`` while "alive", an int once "exited"."""

    def __init__(self) -> None:
        self.exited = False
        self.pid = 4242

    def poll(self):
        return 1 if self.exited else None


def _manager(monkeypatch) -> WebappManager:
    mgr = WebappManager(
        WebappManagerConfig(startup_timeout_seconds=0.05, poll_interval_seconds=0.01)
    )
    mgr._proc = _FakeProc()
    monkeypatch.setattr(mgr, "is_reachable", lambda: False)
    return mgr


class TestWaitUntilReady:
    def test_succeeds_when_reachable_before_deadline(self, monkeypatch):
        mgr = _manager(monkeypatch)
        monkeypatch.setattr(mgr, "is_reachable", lambda: True)
        mgr._wait_until_ready()  # must not raise

    def test_raises_startup_pending_when_still_alive_at_deadline(self, monkeypatch):
        mgr = _manager(monkeypatch)  # is_reachable() always False, proc alive
        with pytest.raises(WebappStartupPending):
            mgr._wait_until_ready()

    def test_raises_plain_runtime_error_when_process_already_exited(self, monkeypatch):
        mgr = _manager(monkeypatch)
        mgr._proc.exited = True
        with pytest.raises(RuntimeError) as exc_info:
            mgr._wait_until_ready()
        assert not isinstance(exc_info.value, WebappStartupPending)


class TestWatchUntilResolved:
    def test_returns_true_once_reachable(self, monkeypatch):
        mgr = _manager(monkeypatch)
        calls = {"n": 0}

        def _is_reachable():
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(mgr, "is_reachable", _is_reachable)
        assert mgr.watch_until_resolved() is True
        assert calls["n"] == 3

    def test_returns_false_when_process_exits_first(self, monkeypatch):
        mgr = _manager(monkeypatch)  # is_reachable() always False
        calls = {"n": 0}

        def _is_reachable():
            calls["n"] += 1
            if calls["n"] == 2:
                mgr._proc.exited = True
            return False

        monkeypatch.setattr(mgr, "is_reachable", _is_reachable)
        assert mgr.watch_until_resolved() is False

    def test_returns_false_immediately_if_proc_already_gone(self, monkeypatch):
        mgr = _manager(monkeypatch)
        mgr._proc = None
        assert mgr.watch_until_resolved() is False


class TestSlowBootIsOurs:
    """A spawned child that is alive but not yet bound is ours (a slow boot)."""

    def _unbound(self, monkeypatch) -> WebappManager:
        mgr = _manager(monkeypatch)  # proc alive, is_reachable() False
        monkeypatch.setattr(mgr, "is_port_in_use", lambda: False)
        return mgr

    def test_status_reports_a_starting_child_as_ours(self, monkeypatch):
        status = self._unbound(monkeypatch).status()
        assert status.running is True
        assert status.ownership == "ours"
        assert "starting" in status.detail

    def test_stop_terminates_a_child_that_has_not_bound_yet(self, monkeypatch):
        mgr = self._unbound(monkeypatch)
        proc = mgr._proc
        terminated: list[str] = []
        proc.send_signal = lambda _sig: None
        proc.terminate = lambda: terminated.append("terminate")
        proc.wait = lambda timeout=None: 0

        mgr.stop()

        assert terminated == ["terminate"]
        assert mgr._proc is None

    def test_start_does_not_spawn_a_second_process_over_a_starting_child(self, monkeypatch):
        mgr = self._unbound(monkeypatch)
        first = mgr._proc
        monkeypatch.setattr(
            "app.webapp.manager.subprocess.Popen",
            lambda *a, **k: pytest.fail("spawned a second webapp"),
        )

        mgr.start(wait=False)

        assert mgr._proc is first


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
class TestPortProbePerPlatform:
    """``is_port_in_use`` reads one ``connect_ex`` code, whose value is per-platform."""

    def test_a_listener_on_the_loopback_port_is_in_use(self, monkeypatch, platform):
        calls = _fake_sockets(monkeypatch, {("127.0.0.1", 8447)}, _CONNECT_EX_REFUSED[platform])

        assert WebappManager(WebappManagerConfig(host="0.0.0.0", port=8447)).is_port_in_use() is True
        # The wildcard bind address is probed on loopback, over a bounded IPv4 TCP connect.
        assert calls == [
            ("socket", socket.AF_INET, socket.SOCK_STREAM),
            ("settimeout", 0.2),
            ("connect_ex", ("127.0.0.1", 8447)),
        ]

    @pytest.mark.parametrize("misses", [_CONNECT_EX_REFUSED, _CONNECT_EX_TIMED_OUT], ids=["refused", "timed-out"])
    def test_a_refused_or_timed_out_connect_is_free(self, monkeypatch, platform, misses):
        _fake_sockets(monkeypatch, set(), misses[platform])

        assert WebappManager(WebappManagerConfig(port=8447)).is_port_in_use() is False

    def test_a_listener_only_on_the_ipv6_loopback_is_not_seen(self, monkeypatch, platform):
        _fake_sockets(monkeypatch, {("::1", 8447)}, _CONNECT_EX_REFUSED[platform])

        assert WebappManager(WebappManagerConfig(port=8447)).is_port_in_use() is False

    def test_a_specific_bind_host_is_probed_as_configured(self, monkeypatch, platform):
        _fake_sockets(monkeypatch, {("192.0.2.10", 8447)}, _CONNECT_EX_REFUSED[platform])

        assert WebappManager(WebappManagerConfig(host="192.0.2.10", port=8447)).is_port_in_use() is True
