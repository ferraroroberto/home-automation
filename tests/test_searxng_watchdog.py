from __future__ import annotations

import asyncio

import pytest

from app.webapp import searxng_watchdog as wdog
from src.searxng_client import SearxngCommandError, SearxngConfigError, SearxngState


def _state(**kwargs) -> SearxngState:
    """A degraded-by-default state, so each test names only what it cares about."""
    defaults = dict(available=False, container_status="exited", reachable=False, url="http://x:8085")
    defaults.update(kwargs)
    return SearxngState(**defaults)


def _healthy() -> SearxngState:
    return _state(available=True, container_status="running", reachable=True, error=None)


def _run(wd, fetch, *, start=None, restart=None, monkeypatch=None, ticks=1):
    """Drive N ticks with the client calls stubbed; returns the recorded actions."""
    actions = []

    def _start():
        actions.append("start")
        return start() if callable(start) else (start or _healthy())

    def _restart():
        actions.append("restart")
        return restart() if callable(restart) else (restart or _healthy())

    monkeypatch.setattr(wdog, "fetch_searxng_state", fetch)
    monkeypatch.setattr(wdog, "start_searxng", _start)
    monkeypatch.setattr(wdog, "restart_searxng", _restart)

    async def _drive():
        for _ in range(ticks):
            await wdog._tick(wd)

    asyncio.run(_drive())
    return actions


def test_exited_container_is_started_without_a_human(monkeypatch) -> None:
    """The headline defect: a stopped container recovers on its own (#716)."""
    wd = wdog._WatchdogState()
    actions = _run(wd, lambda: _state(container_status="exited"), monkeypatch=monkeypatch)

    assert actions == ["start"]
    # A confirmed-available read-back clears the backoff, so the next real
    # outage is retried immediately rather than inheriting a stale delay.
    assert wd.backoff.consecutive_failures == 0
    assert wd.last_error is None


def test_missing_container_is_created(monkeypatch) -> None:
    wd = wdog._WatchdogState()
    actions = _run(wd, lambda: _state(container_status="not_found"), monkeypatch=monkeypatch)
    assert actions == ["start"]


def test_healthy_container_is_left_alone(monkeypatch) -> None:
    wd = wdog._WatchdogState()
    actions = _run(wd, _healthy, monkeypatch=monkeypatch, ticks=3)
    assert actions == []


def test_running_but_unreachable_waits_out_the_grace_window(monkeypatch) -> None:
    """A booting container must not be recreated out from under itself."""
    wd = wdog._WatchdogState()
    unreachable = lambda: _state(container_status="running", reachable=False)  # noqa: E731

    actions = _run(wd, unreachable, monkeypatch=monkeypatch, ticks=wdog.UNREACHABLE_GRACE_TICKS)
    assert actions == [], "acted before the grace window elapsed"

    # One more poll crosses the threshold — now it is wedged, not booting.
    actions = _run(wd, unreachable, monkeypatch=monkeypatch, ticks=1)
    assert actions == ["restart"]


def test_backoff_suppresses_a_retry_inside_the_window(monkeypatch) -> None:
    wd = wdog._WatchdogState()

    def _failing_start():
        raise SearxngCommandError("docker daemon not running")

    actions = _run(
        wd,
        lambda: _state(container_status="exited"),
        start=_failing_start,
        monkeypatch=monkeypatch,
        ticks=4,
    )

    # First tick attempts and fails; the next three land inside the backoff
    # window, so `docker` is not thrashed once per poll.
    assert actions == ["start"]
    assert wd.backoff.consecutive_failures == 1
    assert wd.backoff.seconds_remaining() is not None


def test_a_command_that_exits_zero_without_restoring_service_is_not_a_success(monkeypatch) -> None:
    """`up -d` returning 0 is not the same fact as "search works" (CLAUDE.md)."""
    wd = wdog._WatchdogState()
    actions = _run(
        wd,
        lambda: _state(container_status="exited"),
        start=lambda: _state(container_status="exited", error="still down"),
        monkeypatch=monkeypatch,
    )

    assert actions == ["start"]
    assert wd.backoff.consecutive_failures == 1, "a hollow success was recorded as recovery"


def test_recovery_clears_the_backoff_and_the_unreachable_streak(monkeypatch) -> None:
    wd = wdog._WatchdogState()
    wd.backoff.record_failure()
    wd.last_error = "container is exited"
    wd.unreachable_ticks = 2

    _run(wd, _healthy, monkeypatch=monkeypatch)

    assert wd.backoff.consecutive_failures == 0
    assert wd.backoff.seconds_remaining() is None
    assert wd.unreachable_ticks == 0
    assert wd.last_error is None


def test_a_status_read_failure_never_escapes_the_tick(monkeypatch) -> None:
    wd = wdog._WatchdogState()

    def _boom():
        raise OSError("docker socket gone")

    actions = _run(wd, _boom, monkeypatch=monkeypatch)

    assert actions == []
    assert wd.last_error is not None


def test_watchdog_not_started_when_the_stack_path_is_unconfigured(monkeypatch) -> None:
    def _unset():
        raise SearxngConfigError("SEARXNG_COMPOSE_PATH is not set")

    monkeypatch.setattr(wdog, "compose_path", _unset)
    assert wdog.start_searxng_watchdog() is None


def test_watchdog_starts_a_task_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(wdog, "compose_path", lambda: "C:/stack/docker-compose.yml")

    async def _start_then_cancel():
        task = wdog.start_searxng_watchdog()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_start_then_cancel())
