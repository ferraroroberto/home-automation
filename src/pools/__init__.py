"""Named thread pools for blocking work, one per class of work.

Every blocking call the webapp makes off the event loop (a device read, a
SQLite write, a notification send, a ping) runs on one of these pools instead
of the event loop's single shared default pool. A class of work that stalls
(a cloud API hanging, a ping sweep over every LAN host, a live camera stream
holding a thread per viewer) then fills only its own pool, and every other
class keeps answering.

* :data:`HOT_POLL` — reads the dashboard polls (device and network state).
* :data:`ON_DEMAND` — a person's action: a switch, a reboot, a rename, a write.
* :data:`BACKGROUND` — the automation engines, samplers, notifications and
  their store writes, which run whether or not anyone is looking.
* :data:`PROBE` — bursty fan-out of short blocking calls (one ping per host,
  an mDNS discovery window).
* :data:`STREAM` — a live camera stream's frame hand-off, which blocks for the
  stream's whole lifetime.

Each pool is a :class:`~src.request_timing.TimedExecutor`, so a request's
``Server-Timing`` header splits its queueing from its work per pool. Threads
start lazily, so an idle pool costs nothing.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from typing import Callable, TypeVar

from src.request_timing import TimedExecutor

T = TypeVar("T")

# Sizes: the old shared pool on this host was 20 threads for everything.
# Hot reads fan out per device (every plug, every blind) on one poll, so they
# get the most; a person's actions are few and one at a time.
HOT_POLL = TimedExecutor("hot-poll", 12)
ON_DEMAND = TimedExecutor("on-demand", 6)
BACKGROUND = TimedExecutor("background", 8)
# A ping sweep probes every reachable LAN host at once; each call is short.
PROBE = TimedExecutor("probe", 16)
# One thread per open camera stream for the stream's lifetime.
STREAM = TimedExecutor("stream", 8)


async def run_in(pool: TimedExecutor, fn: Callable[..., T], /, *args: object, **kwargs: object) -> T:
    """Run ``fn(*args, **kwargs)`` on ``pool`` and await its result.

    The context is copied into the worker thread, as ``asyncio.to_thread``
    does, so context variables (the request timing among them) carry over.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await loop.run_in_executor(pool, call)
