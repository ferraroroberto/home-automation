"""Unit tests for the vendored e2e live-instance guard (issue #538).

``tests/e2e/_e2e_live_guard.py`` is copied byte-identical from
project-scaffolding (see ``.fleet.toml`` ``[vendored]``); this covers the
three scenarios this repo's own ``tests/e2e/conftest.py:base_url`` fixture
depends on, all against throwaway ports — never the real tray on :8447.
"""

from __future__ import annotations

import socket

import pytest

from tests.e2e import _e2e_live_guard as guard


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_free_port_boots_fresh_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing listening -> autoboot, the default (and safe) path."""
    monkeypatch.delenv("E2E_LIVE", raising=False)
    port = _reserve_free_port()

    live_opt_in = guard.require_disposable_instance(port, "E2E_LIVE")

    assert live_opt_in is False
    assert "booting disposable instance" in capsys.readouterr().out


def test_occupied_port_without_flag_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare run must never silently adopt an occupied port (issue #538's bug)."""
    monkeypatch.delenv("E2E_LIVE", raising=False)
    port = _reserve_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        with pytest.raises(pytest.exit.Exception, match="E2E_LIVE") as excinfo:
            guard.require_disposable_instance(port, "E2E_LIVE")

    message = str(excinfo.value).lower()
    assert "e2e_live" in message
    assert "acting on the live instance" in message


def test_occupied_port_with_flag_acts_on_live_instance_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit opt-in adopts instead of refusing, and says so."""
    monkeypatch.setenv("E2E_LIVE", "1")
    port = _reserve_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        live_opt_in = guard.require_disposable_instance(port, "E2E_LIVE")

    out = capsys.readouterr().out
    assert live_opt_in is True
    assert "acting on the live instance" in out


def test_port_is_in_use_reflects_a_bound_listener() -> None:
    port = _reserve_free_port()
    assert guard.port_is_in_use(port) is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)

        assert guard.port_is_in_use(port) is True
