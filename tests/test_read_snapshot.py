"""The in-memory read snapshot (#758): staleness bound, demand-gated tick,
and writes that a fetch already in flight can't overwrite."""

from __future__ import annotations

import asyncio
from typing import Dict, List

import pytest

from app.webapp import read_snapshot
from app.webapp.read_snapshot import ReadSnapshot


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(read_snapshot.time, "monotonic", c)
    return c


def _counting_snapshot(values: List[Dict[str, int]]) -> tuple[ReadSnapshot, List[int]]:
    calls: List[int] = []

    async def fetch() -> Dict[str, int]:
        calls.append(1)
        return dict(values[min(len(calls), len(values)) - 1])

    return ReadSnapshot("t", fetch, max_age_s=60, tick_s=30, demand_window_s=60), calls


def test_read_serves_from_memory_and_reports_age(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}])

    async def run() -> None:
        value, meta = await snap.read()
        assert value == {"a": 1} and meta["age_seconds"] == 0.0
        clock.now += 20
        value, meta = await snap.read()
        assert value == {"a": 1}
        assert meta["age_seconds"] == 20.0
        assert meta["built_at"]

    asyncio.run(run())
    assert len(calls) == 1


def test_read_past_the_staleness_bound_fetches_before_answering(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}, {"a": 2}])

    async def run() -> None:
        await snap.read()
        clock.now += 61
        value, meta = await snap.read()
        assert value == {"a": 2}
        assert meta["age_seconds"] == 0.0

    asyncio.run(run())
    assert len(calls) == 2


def test_tick_fetches_only_while_read_within_the_demand_window(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}, {"a": 2}])

    async def run() -> None:
        await snap.tick_once()
        assert calls == []  # never read: nothing upstream
        await snap.read()
        clock.now += 30
        await snap.tick_once()
        assert len(calls) == 2  # read 30 s ago: still demanded
        clock.now += 61
        await snap.tick_once()
        assert len(calls) == 2  # unread for longer than the window

    asyncio.run(run())


def test_update_applies_now_and_survives_a_fetch_already_in_flight(clock: _Clock) -> None:
    calls: List[int] = []

    async def run() -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def fetch() -> Dict[str, int]:
            calls.append(1)
            if len(calls) == 2:
                started.set()
                await release.wait()
                return {"a": 1}  # read upstream before the write below
            return {"a": 1}

        snap = ReadSnapshot("t", fetch, max_age_s=60, tick_s=30)
        await snap.read()
        clock.now += 61  # stale, so the next read fetches
        reader = asyncio.create_task(snap.read())
        await started.wait()
        snap.update(lambda v: {**v, "a": 9})  # the write's read-back
        release.set()
        value, _ = await reader
        assert value == {"a": 9}
        value, _ = await snap.read()
        assert value == {"a": 9}

    asyncio.run(run())


def test_invalidate_refetches_on_the_next_read(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}, {"a": 2}])

    async def run() -> None:
        await snap.read()
        snap.invalidate()
        value, _ = await snap.read()
        assert value == {"a": 2}
        value, _ = await snap.read()
        assert value == {"a": 2}  # clean again: served from memory

    asyncio.run(run())
    assert len(calls) == 2


def test_warm_fetches_once_and_a_later_read_reuses_it(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}])

    async def run() -> None:
        await snap.warm()
        await snap.warm()  # already holding a value: no second cold fetch
        value, _ = await snap.read()
        assert value == {"a": 1}

    asyncio.run(run())
    assert len(calls) == 1


def test_warm_is_not_a_demand_and_survives_a_failing_fetch(clock: _Clock) -> None:
    async def boom() -> Dict[str, int]:
        raise RuntimeError("dongle moved")

    snap = ReadSnapshot("t-warm", boom, max_age_s=60, tick_s=30, demand_window_s=60)

    async def run() -> None:
        await snap.warm()  # swallowed: the first read will fetch inline and raise
        with pytest.raises(RuntimeError):
            await snap.read()

    asyncio.run(run())


def _stale_snapshot(fetch, **kw) -> ReadSnapshot:
    return ReadSnapshot("t-stale", fetch, max_age_s=10, tick_s=5, stale_after_s=120, **kw)


def test_serve_stale_answers_past_the_bound_without_fetching_inline(clock: _Clock) -> None:
    """#771: a read past ``max_age_s`` returns the held value and refreshes behind it."""
    calls: List[int] = []

    async def fetch() -> Dict[str, int]:
        calls.append(1)
        await asyncio.sleep(0)
        return {"a": len(calls)}

    snap = _stale_snapshot(fetch)

    async def run() -> None:
        await snap.read()  # nothing to serve yet: the one inline fetch
        clock.now += 11
        value, meta = await snap.read()
        assert value == {"a": 1} and meta["age_seconds"] == 11.0 and meta["stale"] is False
        await snap._background  # the refresh the read left behind
        value, meta = await snap.read()
        assert value == {"a": 2} and meta["age_seconds"] == 0.0

    asyncio.run(run())
    assert len(calls) == 2


def test_serve_stale_readers_share_one_background_refresh(clock: _Clock) -> None:
    calls: List[int] = []

    async def fetch() -> Dict[str, int]:
        calls.append(1)
        await asyncio.sleep(0)
        return {"a": len(calls)}

    snap = _stale_snapshot(fetch)

    async def run() -> None:
        await snap.read()
        clock.now += 11
        await asyncio.gather(*(snap.read() for _ in range(5)))
        await snap._background

    asyncio.run(run())
    assert len(calls) == 2


def test_serve_stale_marks_a_dead_upstream_stale_with_its_error(clock: _Clock) -> None:
    """Old data past the ceiling is its own state, not folded into "fine"."""
    fail = {"on": False}

    async def fetch() -> Dict[str, int]:
        if fail["on"]:
            raise RuntimeError("dongle moved")
        return {"a": 1}

    snap = _stale_snapshot(fetch)

    async def run() -> None:
        _, meta = await snap.read()
        assert meta["stale"] is False and meta["error"] is None
        fail["on"] = True
        clock.now += 121
        value, meta = await snap.read()  # still answers, with the last good reading
        assert value == {"a": 1} and meta["stale"] is True
        await snap._background
        _, meta = await snap.read()
        assert meta["stale"] is True and meta["error"] == "dongle moved"
        fail["on"] = False
        clock.now += 6
        await snap.read()
        await snap._background
        _, meta = await snap.read()
        assert meta["stale"] is False and meta["error"] is None

    asyncio.run(run())


def test_serve_stale_does_not_retry_a_failing_upstream_per_request(clock: _Clock) -> None:
    calls: List[int] = []

    async def fetch() -> Dict[str, int]:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("down")
        return {"a": 1}

    snap = _stale_snapshot(fetch)

    async def run() -> None:
        await snap.read()
        clock.now += 11
        await snap.read()
        await snap._background
        for _ in range(10):  # a burst of reads inside tick_s
            await snap.read()
        assert snap._background.done()

    asyncio.run(run())
    assert len(calls) == 2


def test_serve_stale_still_refetches_inline_after_a_write(clock: _Clock) -> None:
    snap, calls = _counting_snapshot([{"a": 1}, {"a": 2}])
    snap.stale_after_s = 120

    async def run() -> None:
        await snap.read()
        snap.invalidate()
        value, _ = await snap.read()
        assert value == {"a": 2}

    asyncio.run(run())
