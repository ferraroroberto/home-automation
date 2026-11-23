"""A stalled thread pool starves only its own class of work.

Every background worker on :data:`src.pools.BACKGROUND` is held on an event,
with one more call queued behind them; a dashboard read on
:data:`src.pools.HOT_POLL` must still answer, and its ``Server-Timing`` header
must name the pool it ran on. Nothing here reaches a device: the read is the
activity log over a stubbed store.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient

from src import pools, telemetry

_HOLD_S = 10.0


@pytest.fixture
def stalled_background() -> Iterator[Future]:
    """Fill every BACKGROUND worker, plus one queued call; release on teardown."""
    release = threading.Event()
    started = threading.Semaphore(0)

    def hold() -> None:
        started.release()
        release.wait(_HOLD_S)

    holders: List[Future] = [pools.BACKGROUND.submit(hold) for _ in range(pools.BACKGROUND._max_workers)]
    for _ in holders:
        assert started.acquire(timeout=_HOLD_S), "a BACKGROUND worker never started"
    queued = pools.BACKGROUND.submit(lambda: None)
    try:
        yield queued
    finally:
        release.set()
        for fut in [*holders, queued]:
            fut.result(timeout=_HOLD_S)


def test_hot_read_answers_while_background_pool_is_stalled(
    client: TestClient, stalled_background: Future, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telemetry, "read_events", lambda **_: [{"domain": "alarm"}])

    resp = client.get("/api/activity/domains")

    assert resp.status_code == 200
    assert resp.json() == {"domains": ["alarm"]}
    assert 'desc="hot-poll"' in resp.headers["server-timing"]
    # The stall was real: the call queued behind the held workers has not run.
    assert not stalled_background.done()
