"""Event-loop stall probe shared by the "never blocks the loop" regressions (#635).

A coroutine that does its blocking I/O inline pins uvicorn's single loop for
the whole call; one that threads the call off keeps waking on schedule.
"""

from __future__ import annotations

import asyncio
import time

#: How long the faked blocking send sleeps; the loop may stall for under half.
BLOCK_S = 0.4


async def run_watching_the_loop(coro) -> tuple[float, float]:
    """Await ``coro`` while heart-beating; return (worst wakeup gap, elapsed).

    A coroutine that does its blocking I/O inline pins the loop for the whole
    duration, so the heartbeat's worst gap ≈ the block; one that threads the
    call off keeps waking on schedule.
    """

    gaps: list[float] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        await coro
    finally:
        elapsed = time.monotonic() - started
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass
    return max(gaps, default=elapsed), elapsed
