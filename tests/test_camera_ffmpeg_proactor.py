"""Regression pin for issue #399: ffmpeg subprocess calls must survive a
selector-loop uvicorn server.

Issue #397 moved the webapp's own uvicorn loop to ``asyncio.SelectorEventLoop``
on Windows (the proactor loop kills its listening socket on any aborted client
connection). But Windows only implements ``asyncio`` subprocess transports on
the *proactor* loop — ``asyncio.create_subprocess_exec`` called directly on a
running selector loop raises a bare ``NotImplementedError``, which is exactly
what ``src.camera_ffmpeg``'s ``snapshot`` / ``mjpeg_frames`` / ``start_record``
do under the hood. These tests pin the fix (a persistent background-thread
proactor loop every ffmpeg call is dispatched to via ``_run_on_proactor``),
not the camera hardware — no RTSP/ONVIF I/O here, just real ``ffmpeg``-free
subprocess calls against the current interpreter.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from src import camera_ffmpeg as F
from src._no_window import NO_WINDOW

_SELECTOR_LOOP_FACTORY = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop


async def _echo() -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('hi', end='')",
        stdout=asyncio.subprocess.PIPE, creationflags=NO_WINDOW,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out


@pytest.mark.skipif(sys.platform != "win32", reason="selector-loop subprocess gap is Windows-only")
def test_bare_subprocess_exec_fails_under_selector_loop() -> None:
    """Documents the bug this issue fixes — pin stays until CPython closes the gap."""

    async def _direct() -> None:
        await _echo()

    with pytest.raises(NotImplementedError):
        asyncio.run(_direct(), loop_factory=asyncio.SelectorEventLoop)


def test_run_on_proactor_survives_selector_loop() -> None:
    """The actual fix: the proactor bridge succeeds even though the calling
    loop is the selector loop uvicorn now runs under (issue #397)."""

    async def _caller() -> tuple[int, bytes]:
        return await F._run_on_proactor(_echo)

    returncode, out = asyncio.run(_caller(), loop_factory=_SELECTOR_LOOP_FACTORY)
    assert returncode == 0
    assert out == b"hi"


def test_run_on_proactor_reuses_one_persistent_loop() -> None:
    """The background loop is started lazily, once — not spun up per call."""

    async def _caller() -> asyncio.AbstractEventLoop:
        return F._ensure_proactor_loop()

    first = asyncio.run(_caller(), loop_factory=_SELECTOR_LOOP_FACTORY)
    second = asyncio.run(_caller(), loop_factory=_SELECTOR_LOOP_FACTORY)
    assert first is second


class _KillableStdout:
    """A pipe that floods frames, idles until ``kill``, then flushes more and EOFs.

    The post-kill flush refills the bounded frame queue after the consumer has
    already gone — the exact state in which the pump's closing sentinel used to
    block forever.
    """

    _FRAME = b"\xff\xd8" + b"x" * 16

    def __init__(self) -> None:
        self.killed = False
        self._reads = 0

    async def read(self, _n: int) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return self._FRAME * 8
        while not self.killed:
            await asyncio.sleep(0.01)
        if self._reads == 2:
            return self._FRAME * 10
        return b""


class _KillableProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = _KillableStdout()

    def kill(self) -> None:
        self.stdout.killed = True

    async def wait(self) -> int:
        return 0


def test_mjpeg_viewer_leaving_does_not_wedge_proactor_loop(monkeypatch) -> None:
    """Closing the live view with a full frame queue must leave the shared
    proactor loop usable (audit #916): the pump's closing sentinel used to be a
    blocking ``Queue.put`` on the loop thread, stalling every later camera call."""
    from src import camera_client

    async def fake_exec(*_a, **_kw) -> _KillableProc:
        return _KillableProc()

    async def fake_url(_cfg, *, main: bool) -> str:
        return "rtsp://192.0.2.10:554/sub"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(camera_client, "get_camera_config", lambda *_a, **_kw: object())
    monkeypatch.setattr(camera_client, "_stream_url_for", fake_url)

    async def _noop() -> str:
        return "alive"

    async def _scenario() -> str:
        gen = F.mjpeg_frames("garden")
        await gen.__anext__()  # one frame, then the viewer leaves
        await gen.aclose()
        return await asyncio.wait_for(F._run_on_proactor(_noop), timeout=3.0)

    assert asyncio.run(_scenario(), loop_factory=_SELECTOR_LOOP_FACTORY) == "alive"
