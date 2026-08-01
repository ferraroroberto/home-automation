"""The shared Windows no-window spawn flag (issue #572).

Two things worth locking down. First, the constant itself: `NO_WINDOW` must be
`CREATE_NO_WINDOW` on Windows and exactly `0` elsewhere — a non-zero
`creationflags` is a `ValueError` on POSIX, and the whole point of the shared
constant is that call sites can pass it unconditionally.

Second, `scripts/gen_tailscale_cert.py`: it is spawned from
`WebappManager._renew_tailscale_cert` on **every** webapp start, from the
windowless tray, and both of its own `tailscale` spawns used to omit
`creationflags` entirely — so each start flashed a console window at whoever was
at the machine. These tests fail against the pre-fix code.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List

from scripts import gen_tailscale_cert
from src._no_window import NO_WINDOW


def test_no_window_is_create_no_window_on_windows_and_zero_elsewhere() -> None:
    if sys.platform == "win32":
        assert NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        assert NO_WINDOW == 0


def _record_runs(monkeypatch, stdout: str) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(gen_tailscale_cert.subprocess, "run", fake_run)
    return calls


def test_tailscale_status_spawn_is_windowless(monkeypatch) -> None:
    calls = _record_runs(monkeypatch, '{"Self": {"DNSName": "tower.example.ts.net."}}')

    assert gen_tailscale_cert._tailscale_hostname() == "tower.example.ts.net"

    assert calls[0]["cmd"] == ["tailscale", "status", "--json"]
    assert calls[0]["creationflags"] == NO_WINDOW


def test_cert_provision_spawn_is_windowless(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gen_tailscale_cert, "CERT_DIR", tmp_path / "certificates")
    calls = _record_runs(monkeypatch, "")

    gen_tailscale_cert._provision("tower.example.ts.net")

    assert calls[0]["cmd"][:2] == ["tailscale", "cert"]
    assert calls[0]["creationflags"] == NO_WINDOW
