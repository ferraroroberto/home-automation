"""Unit tests for :mod:`src.host_shutdown` (#962).

Nothing here may reach the real ``shutdown`` command: the first test proves the
pytest hard-block on every initiate path; the rest fake ``subprocess.run`` and
lift only the ``"pytest" in sys.modules`` guard via ``_unblock``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, List

import pytest

from src import host_shutdown


def test_initiate_calls_are_blocked_under_pytest(monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a real shutdown command was spawned under pytest")

    monkeypatch.setattr(host_shutdown.subprocess, "run", boom)
    assert host_shutdown.initiate_shutdown() is False
    assert host_shutdown.initiate_hibernate() is False
    assert host_shutdown.cancel_shutdown() is False


@pytest.fixture
def _unblock(monkeypatch):
    """Lift the pytest guard for the code under test only; ``subprocess.run`` is
    always faked, so no OS command can run."""
    monkeypatch.delitem(sys.modules, "pytest")
    yield
    sys.modules["pytest"] = pytest


def _fake_run(monkeypatch, returncode: int, calls: List[dict]) -> None:
    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="nope")

    monkeypatch.setattr(host_shutdown.subprocess, "run", run)


def test_initiate_hibernate_runs_shutdown_h_without_a_window(monkeypatch, _unblock) -> None:
    calls: List[dict] = []
    _fake_run(monkeypatch, 0, calls)
    assert host_shutdown.initiate_hibernate() is True
    assert calls[0]["cmd"] == ["shutdown", "/h"]  # no /t: /h takes no timeout
    assert calls[0]["creationflags"] == host_shutdown.NO_WINDOW


def test_initiate_hibernate_false_when_windows_refuses(monkeypatch, _unblock) -> None:
    _fake_run(monkeypatch, 1, [])  # e.g. hibernation not enabled
    assert host_shutdown.initiate_hibernate() is False


def test_initiate_hibernate_never_raises_on_oserror(monkeypatch, _unblock) -> None:
    def run(*_a: Any, **_k: Any):
        raise OSError("no shutdown.exe")

    monkeypatch.setattr(host_shutdown.subprocess, "run", run)
    assert host_shutdown.initiate_hibernate() is False
