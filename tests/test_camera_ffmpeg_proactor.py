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

_SELECTOR_LOOP_FACTORY = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop


async def _echo() -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('hi', end='')",
        stdout=asyncio.subprocess.PIPE,
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
