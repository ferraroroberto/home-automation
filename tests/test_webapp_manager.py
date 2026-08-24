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

import pytest

from app.webapp.manager import WebappManager, WebappManagerConfig, WebappStartupPending


class _FakeProc:
    """Stand-in for the ``subprocess.Popen`` handle — ``poll()`` returns
    ``None`` while "alive", an int once "exited"."""

    def __init__(self) -> None:
        self.exited = False

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
