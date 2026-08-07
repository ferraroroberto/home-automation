"""The shared Windows no-window spawn flag (issues #572, #633).

Three things worth locking down. First, the constant itself: `NO_WINDOW` must be
`CREATE_NO_WINDOW` on Windows and exactly `0` elsewhere — a non-zero
`creationflags` is a `ValueError` on POSIX, and the whole point of the shared
constant is that call sites can pass it unconditionally.

Second, `scripts/gen_tailscale_cert.py`: it is spawned from
`WebappManager._renew_tailscale_cert` on **every** webapp start, from the
windowless tray, and both of its own `tailscale` spawns used to omit
`creationflags` entirely — so each start flashed a console window at whoever was
at the machine.

Third, `src/camera_ffmpeg.py` (issue #633): its three `ffmpeg` spawns are the
repo's most frequent short-lived children — one per snapshot, one per live-view
pump, one per recording — and were the last call sites in the repo still missing
the flag. They go through `asyncio.create_subprocess_exec`, which forwards the
kwarg to `subprocess.Popen` like any other, so the miss was an oversight rather
than a platform limitation.

All of these tests fail against the pre-fix code.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from scripts import gen_tailscale_cert
from src import camera_client
from src import camera_ffmpeg
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


# --------------------------------------------------------------------------- #
# camera_ffmpeg's three ffmpeg spawns (issue #633)                            #
# --------------------------------------------------------------------------- #
class _FakeStdout:
    """An immediately-EOF pipe, so the MJPEG pump exits after one read."""

    async def read(self, _n: int) -> bytes:
        return b""


class _FakeProc:
    """Just enough of ``asyncio.subprocess.Process`` for the three capture paths."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = _FakeStdout()
        self.stdin = None

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"\xff\xd8fake-jpeg", b""

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def _record_spawns(monkeypatch) -> List[Dict[str, Any]]:
    """Capture every ``create_subprocess_exec`` call without launching ffmpeg."""
    calls: List[Dict[str, Any]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append({"argv": args, **kwargs})
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _camera_config(tmp_path: Path, monkeypatch) -> Path:
    """A one-camera config file + a stubbed RTSP URL (no ONVIF, no network)."""
    cfg_path = tmp_path / "cameras.json"
    cfg_path.write_text(
        json.dumps([{"id": "garden", "host": "192.0.2.10", "username": "a", "password": "b"}]),
        encoding="utf-8",
    )

    async def fake_url(cfg: Any, *, main: bool) -> str:
        return "rtsp://192.0.2.10:554/sub"

    monkeypatch.setattr(camera_client, "_stream_url_for", fake_url)
    return cfg_path


def test_snapshot_ffmpeg_spawn_is_windowless(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _camera_config(tmp_path, monkeypatch)
    monkeypatch.setattr(camera_ffmpeg, "LAST_SNAPSHOT_DIR", tmp_path / "last")
    calls = _record_spawns(monkeypatch)

    frame = asyncio.run(camera_ffmpeg.snapshot("garden", path=cfg_path))

    assert frame == b"\xff\xd8fake-jpeg"
    assert len(calls) == 1
    assert calls[0]["argv"][0] == "ffmpeg"
    assert calls[0]["creationflags"] == NO_WINDOW


def test_mjpeg_stream_ffmpeg_spawn_is_windowless(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _camera_config(tmp_path, monkeypatch)
    calls = _record_spawns(monkeypatch)

    async def _drain() -> List[bytes]:
        return [frame async for frame in camera_ffmpeg.mjpeg_frames("garden", path=cfg_path)]

    assert asyncio.run(_drain()) == []  # EOF straight away — we only want the spawn
    assert len(calls) == 1
    assert calls[0]["argv"][0] == "ffmpeg"
    assert calls[0]["creationflags"] == NO_WINDOW


def test_record_ffmpeg_spawn_is_windowless(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _camera_config(tmp_path, monkeypatch)
    monkeypatch.setattr(camera_ffmpeg, "CAPTURE_DIR", tmp_path / "captures")
    monkeypatch.setattr(camera_ffmpeg, "_recordings", {})
    calls = _record_spawns(monkeypatch)

    name = asyncio.run(camera_ffmpeg.start_record("garden", path=cfg_path))

    assert name.startswith("garden-") and name.endswith(".mp4")
    assert len(calls) == 1
    assert calls[0]["argv"][0] == "ffmpeg"
    assert calls[0]["creationflags"] == NO_WINDOW
