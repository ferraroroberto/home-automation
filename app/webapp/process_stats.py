"""Periodic process-health breadcrumb: thread, handle and memory counts.

A slow resource leak (a worker thread or handle per tick) is invisible in the
request log and takes days to bite. One info line every ``PROCESS_STATS_INTERVAL_S``
seconds (default 300; ``0`` turns it off) makes the growth readable straight
from ``webapp.log``, e.g. ``threads=41 handles=612 rss=190MB``.

Read-only and cheap, so every instance runs it, engines on or off. ``threads``
counts Python threads (every thread this app spawns is one); ``handles`` and
``rss`` come from the Windows process APIs and read ``?`` where they are
unavailable.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Optional, Tuple

from dotenv import load_dotenv

from app.webapp._env import _env_int
from app.webapp._task_loop import run_loop

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 300


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_handles_and_rss() -> Tuple[Optional[int], Optional[int]]:
    """(open handle count, working-set bytes) for this process, ``None`` where unreadable."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    process = kernel32.GetCurrentProcess()

    handles: Optional[int] = None
    count = wintypes.DWORD()
    if kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
        handles = int(count.value)

    rss: Optional[int] = None
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        rss = int(counters.WorkingSetSize)
    return handles, rss


def describe_process() -> str:
    """The ``threads=… handles=… rss=…`` fragment for one log line."""
    handles: Optional[int] = None
    rss: Optional[int] = None
    if sys.platform == "win32":
        try:
            handles, rss = _windows_handles_and_rss()
        except Exception as exc:  # noqa: BLE001 — a breadcrumb must never break the loop
            logger.debug("process handle/rss read failed: %s", exc)
    return "threads=%d handles=%s rss=%s" % (
        threading.active_count(),
        "?" if handles is None else handles,
        "?" if rss is None else "%dMB" % (rss // (1024 * 1024)),
    )


async def _tick() -> None:
    logger.info("ℹ️ Process health: %s", describe_process())


def start_process_stats() -> "asyncio.Task | None":
    """Start the periodic breadcrumb; return the task (``None`` when interval is 0)."""
    load_dotenv(override=True)
    interval_s = _env_int("PROCESS_STATS_INTERVAL_S", _DEFAULT_INTERVAL_S, non_negative=True)
    if interval_s == 0:
        logger.info("ℹ️ Process health log disabled (PROCESS_STATS_INTERVAL_S=0)")
        return None
    return asyncio.create_task(
        run_loop(
            _tick,
            interval_s,
            logger=logger,
            name="Process health log",
            start_msg="ℹ️ Process health log started (every %ds): %s" % (interval_s, describe_process()),
        ),
        name="process-stats",
    )
