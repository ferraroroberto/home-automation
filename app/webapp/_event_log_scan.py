"""One RISCO event-log scanner for the alarm engines (issue #912).

``alarm_scene_automation`` (#325) and ``security_override_automation`` (#341)
both act on RISCO's event log rather than its live alarm flags: diff
``fetch_events()`` against a disk-persisted cursor, re-check that cursor
immediately before claiming each new event so two concurrent scans (an
orphaned process after a tray restart) can't both act on one (#339), throttle
how often the log is read and never let two scans overlap in one process.
This module holds that workflow once; each engine supplies its cursor store,
which events it wants and what to do with each.

The engines differ in one way, kept as ``claim_before_handle``:

* ``True`` (alarm scene): the claim is saved *before* the handler runs, which
  shrinks the cross-process race window to a couple of file reads. A handler
  that fails is never retried; its event is already claimed.
* ``False`` (alarm override): the handler runs first and the claim is saved
  after, because the handler itself rewrites the store the cursor lives in. A
  handler that raises leaves the cursor where it was, so the next scan
  retries that event.

The scanner logs through the engine's own logger and never catches a handler
failure itself: the override engine wraps its whole scan, the scene engine's
onset handler isolates its own failures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

# pyrisco's event-log ``type`` for a genuine "system has gone off" alarm entry
# (as opposed to e.g. "zone bypassed" / "zone omitted"). Confirmed live against
# production RISCO events.
ALARM_EVENT_TYPE = "triggered"


class EventLogScanner:
    """Throttle, overlap guard and cursor claim for one event-log consumer.

    Holds process-lifetime cadence state only; scan progress lives in the
    engine's on-disk cursor, so a restart resumes where it left off.
    """

    def __init__(self, *, logger: logging.Logger, read_fail_msg: str, claim_before_handle: bool) -> None:
        self._logger = logger
        self._read_fail_msg = read_fail_msg
        self._claim_before_handle = claim_before_handle
        self.last_scan: Optional[float] = None
        self.running = False

    def due(self, interval_s: float, *, now: Optional[float] = None) -> bool:
        """True (and the window restarts) when ``interval_s`` has passed since the last due scan."""

        instant = time.monotonic() if now is None else now
        if self.last_scan is not None and (instant - self.last_scan) < interval_s:
            return False
        self.last_scan = instant
        return True

    async def guarded(self, body: Callable[[], Awaitable[None]]) -> None:
        """Run ``body`` unless a scan is already in flight in this process.

        A slow handler (capture + vision) can outlast one throttle window.
        """

        if self.running:
            return
        self.running = True
        try:
            await body()
        finally:
            self.running = False

    async def scan(
        self,
        *,
        fetch: Callable[[], Awaitable[Iterable[Any]]],
        wanted: Callable[[Any], bool],
        load_cursor: Callable[[], Optional[str]],
        save_cursor: Callable[[str], None],
        handle: Callable[[Any], Awaitable[None]],
    ) -> None:
        """Hand every wanted event newer than the cursor to ``handle``, oldest first."""

        try:
            events = await fetch()
        except Exception as exc:  # noqa: BLE001 — a detached task must never crash silently
            self._logger.warning(self._read_fail_msg, exc)
            return

        relevant = sorted((e for e in events if wanted(e)), key=lambda e: e.time)
        if not relevant:
            return

        cursor = load_cursor()
        if cursor is None:
            # First run ever: establish the baseline without replaying
            # fetch_events()'s full history window as new events.
            save_cursor(relevant[-1].time)
            return

        for event in [e for e in relevant if e.time > cursor]:
            # Re-check the persisted cursor right before claiming: a concurrent
            # scan may have read the same stale cursor and already claimed
            # this event. Confirmed live: two deliveries of one
            # alarm a few hundred ms to ~5 s apart.
            latest = load_cursor()
            if latest is not None and latest >= event.time:
                continue
            if self._claim_before_handle:
                save_cursor(event.time)
                await handle(event)
            else:
                await handle(event)
                save_cursor(event.time)
