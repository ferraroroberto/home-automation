"""Shared background-loop shell for the webapp's ``start_*`` tasks (issue #452).

``automation`` / ``presence_automation`` / ``presence_refresher`` /
``power_monitor`` / ``security_automation`` / ``wake_alarm_automation`` /
``sampler`` / ``telemetry_sampler`` each hand-rolled the identical
``while True: try/except-log/sleep`` shell, wrapped in an outer
``try/except asyncio.CancelledError`` that logs a stop message and
re-raises so the task actually stops. Centralizing it here means the
shell (and its cancellation semantics) can't drift between loops; each
caller keeps its own tick logic, interval, and log message text.

:func:`spawn_background_task` is the matching home for fire-and-forget work
(issue #912): the alarm-scene and alarm-override event scans and the blind
retries each carried their own copy of it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Coroutine, Optional, Set

TickFn = Callable[[], Awaitable[None]]

# Strong references to detached tasks. asyncio keeps only a weak reference to
# a task whose `create_task()` return value is discarded, so it can be GC'd
# mid-flight, before its `finally` runs: that wedged the alarm-scene scan's
# overlap guard closed forever. Tests drain it to await the work.
BACKGROUND_TASKS: Set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """``asyncio.create_task`` that can't be silently GC'd before it finishes."""

    task = asyncio.create_task(coro, name=name)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


async def run_loop(
    tick_fn: TickFn,
    interval_s: float,
    *,
    logger: logging.Logger,
    name: str,
    start_msg: str,
    tick_fail_msg: Optional[str] = None,
) -> None:
    """Run ``tick_fn`` every ``interval_s`` seconds until cancelled.

    ``start_msg`` is logged once (already fully formatted) before the loop
    starts; on cancellation, ``"🛑 %s stopped"`` is logged with ``name`` and
    the ``CancelledError`` is re-raised so the task actually stops.

    ``tick_fail_msg`` is an optional ``%s``-style template logged (at
    warning level, with the exception as the one argument) when ``tick_fn``
    raises — pass ``None`` (the default) when ``tick_fn`` already isolates
    its own failures (e.g. a sampler that logs per-domain), so an
    unexpected exception still propagates instead of being silently
    swallowed.
    """
    logger.info(start_msg)
    try:
        while True:
            if tick_fail_msg is None:
                await tick_fn()
            else:
                try:
                    await tick_fn()
                except Exception as exc:  # noqa: BLE001 — a tick failure never kills the loop
                    logger.warning(tick_fail_msg, exc)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("🛑 %s stopped", name)
        raise
