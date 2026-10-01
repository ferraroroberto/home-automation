"""In-memory snapshot of a slow read, served off the request path (#758).

A read that goes to the cloud on every request (MELCloud's ``/api/units``
logs in from scratch each time, ~1.6 s) is answered from memory instead.

**Freshness.** :meth:`ReadSnapshot.tick_forever` (started from the webapp
lifespan) refetches every ``tick_s`` while the endpoint was read within
``demand_window_s``, and idles otherwise, so an unwatched app polls nothing
upstream. A read that finds the snapshot older than ``max_age_s`` (the first
read after an idle spell, or a tick that keeps failing) fetches inline before
answering: old state is never served as current. Every answer carries its age
(:meth:`ReadSnapshot.describe`), stamped from the moment the fetch *started*,
so the age is that of the oldest data in it.

**Writes.** A write in this process either applies its read-back as a patch
(:meth:`ReadSnapshot.update`) or marks the snapshot dirty
(:meth:`ReadSnapshot.invalidate`) so the next read refetches. Both are
sequence-numbered: a fetch already in flight when a write lands returns data
from before it, so on install the patches newer than the fetch's start are
re-applied on top, and a dirty mark newer than it survives. That is the
lost-update case app-launcher#1345 hit between overlapping reads and marks.

Every snapshot registers under its name, so a writer outside the endpoint's
router (an automation loop) marks it dirty with :func:`invalidate` without
importing the router, and the lifespan starts every tick from
:func:`registered`.

Everything runs on the webapp's one event loop, so the state needs no thread
lock; the asyncio lock only makes concurrent readers share one fetch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Generic, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_REGISTRY: Dict[str, "ReadSnapshot[Any]"] = {}


def registered() -> List["ReadSnapshot[Any]"]:
    """Every snapshot constructed so far (each wired router declares one)."""
    return list(_REGISTRY.values())


def invalidate(name: str) -> None:
    """Mark the named snapshot dirty; a no-op if its router isn't loaded."""
    snap = _REGISTRY.get(name)
    if snap is not None:
        snap.invalidate()


class ReadSnapshot(Generic[T]):
    """One slow read, cached with a staleness bound and a demand-gated tick."""

    def __init__(
        self,
        name: str,
        fetch: Callable[[], Awaitable[T]],
        *,
        max_age_s: float,
        tick_s: float,
        demand_window_s: float = 60.0,
    ) -> None:
        self.name = name
        self._fetch = fetch
        self.max_age_s = max_age_s
        self.tick_s = tick_s
        self.demand_window_s = demand_window_s
        self._lock = asyncio.Lock()
        self.reset()
        _REGISTRY[name] = self

    def reset(self) -> None:
        """Drop all state (tests, and construction)."""
        self._value: Optional[T] = None
        self._built = 0.0  # monotonic, at fetch start
        self._built_at = ""
        self._seq = 0
        self._patches: List[Tuple[int, Callable[[T], T]]] = []
        self._dirty_seq = 0
        self._last_demand = float("-inf")

    def _needs_fetch(self) -> bool:
        return (
            self._value is None
            or self._dirty_seq > 0
            or time.monotonic() - self._built > self.max_age_s
        )

    async def read(self) -> Tuple[T, Dict[str, Any]]:
        """The current value and its age, fetching inline when it is too old.

        A fetch error propagates to the caller, exactly as an uncached read.
        """
        self._last_demand = time.monotonic()
        if self._needs_fetch():
            async with self._lock:
                if self._needs_fetch():  # another reader may have just fetched
                    await self._refresh("inline")
        assert self._value is not None
        return self._value, self.describe()

    async def _refresh(self, reason: str) -> None:
        """Fetch and install. Caller holds ``self._lock``."""
        start_seq = self._seq
        start = time.monotonic()
        built_at = datetime.now().astimezone().isoformat(timespec="seconds")
        value = await self._fetch()
        # Writes that landed while the fetch was in flight are newer than it.
        newer = [(s, p) for s, p in self._patches if s > start_seq]
        for _, patch in newer:
            value = patch(value)
        self._patches = newer
        if self._dirty_seq <= start_seq:
            self._dirty_seq = 0
        self._value, self._built, self._built_at = value, start, built_at
        logger.debug("%s snapshot refreshed (%s) in %.2fs", self.name, reason, time.monotonic() - start)

    def update(self, patch: Callable[[T], T]) -> None:
        """Apply a write's read-back now, and to any fetch still in flight."""
        self._seq += 1
        self._patches.append((self._seq, patch))
        if self._value is not None:
            self._value = patch(self._value)

    def invalidate(self) -> None:
        """A write whose result isn't at hand: the next read refetches."""
        self._seq += 1
        self._dirty_seq = self._seq

    def describe(self) -> Dict[str, Any]:
        """The additive ``snapshot`` key a wired endpoint returns."""
        return {
            "built_at": self._built_at,
            "age_seconds": round(max(0.0, time.monotonic() - self._built), 1),
        }

    async def tick_once(self) -> None:
        """Refetch if someone read recently and nobody just did."""
        if time.monotonic() - self._last_demand > self.demand_window_s:
            return
        async with self._lock:
            if self._value is not None and self._dirty_seq == 0 and (
                time.monotonic() - self._built < self.tick_s / 2
            ):
                return  # a read just fetched inline
            await self._refresh("tick")

    async def tick_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001 — a failed tick must not end the loop
                logger.warning(
                    "⚠️ %s snapshot tick failed; reads past %.0fs fetch inline: %s",
                    self.name, self.max_age_s, exc,
                )
