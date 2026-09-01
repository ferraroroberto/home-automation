"""SearXNG backend supervision for the voice assistant's Tier-3 web search (issue #716).

The `web_search` function behind "Okay Nabu" is only as live as the SearXNG
container backing it, and when that container is down the failure is *silent
and confidently wrong*: the `qwen3.5-4b-nothink` brain falls back to its
training cutoff and answers anyway. #716 caught it answering "GPT-4.0" for
"what's the latest model from OpenAI" — the container had exited cleanly eight
days earlier and nothing had noticed. Docker's own `restart: unless-stopped`
could not help (it deliberately honours a graceful stop forever), and no
restart policy of any kind catches the other shape: running but wedged,
not answering `/healthz`.

So the process that already knows how to read that distinction supervises it.
This adds no new privilege — `start_searxng` is the same code path the Home
card's Start button has always exposed; the watchdog just stops requiring a
human to be looking.

Two failure shapes, two responses:

* **Not running** (`exited` / `created` / `not_found` / `paused`) — `docker
  compose up -d`, immediately.
* **Running but unreachable** — wait out :data:`UNREACHABLE_GRACE_TICKS` polls
  first, because that is also exactly what a healthy container looks like
  while it boots, and only then `docker compose restart`. Recreating a
  container that was merely still starting would turn a 10-second blip into a
  loop.

Every attempt is gated by a shared :class:`~src._backoff.BackoffTracker`, so a
genuinely broken stack backs off to a 30-minute retry instead of thrashing
`docker` every poll, and logging is state-change-only (the
``ha_trace_collector`` ``last_error`` idiom) so an outage costs a handful of
lines, not one per tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.webapp._task_loop import run_loop
from src._backoff import BackoffTracker
from src.searxng_client import (
    SearxngCommandError,
    SearxngConfigError,
    SearxngState,
    compose_path,
    fetch_searxng_state,
    restart_searxng,
    start_searxng,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 120

# How many consecutive running-but-unreachable polls to tolerate before
# treating the container as wedged rather than still booting. At the 120 s
# poll this is a ~6-minute grace window — far longer than SearXNG's real
# startup, so a normal boot is never interrupted.
UNREACHABLE_GRACE_TICKS = 3

# Backoff between recovery attempts. Deliberately coarser than the poll: a
# stack that will not come up is usually broken in a way another `docker
# compose up` cannot fix, so escalate to a half-hourly retry rather than
# hammering the daemon.
BACKOFF_BASE_S = 120.0
BACKOFF_MAX_S = 1800.0

# Container states that a plain `up -d` can genuinely recover.
_STARTABLE = frozenset({"exited", "created", "paused", "dead", "not_found"})


@dataclass
class _WatchdogState:
    """Cross-tick memory: what we last logged, and how long it has been sick."""

    backoff: BackoffTracker = field(
        default_factory=lambda: BackoffTracker(base_s=BACKOFF_BASE_S, max_s=BACKOFF_MAX_S)
    )
    # Last degraded-state text logged, so repeat polls stay quiet. ``None``
    # means "currently believed healthy" — the recovery line is emitted on the
    # transition out of a non-``None`` value, never on a first healthy poll.
    last_error: Optional[str] = None
    unreachable_ticks: int = 0


def _describe(state: SearxngState) -> str:
    """Short, stable text for one degraded state — the log de-dup key."""
    return state.error or f"container is {state.container_status}"


def _plan(state: SearxngState, wd: _WatchdogState) -> Optional[str]:
    """Decide the recovery action for a degraded state: ``"start"``, ``"restart"``, or none.

    Returns ``None`` while a running-but-unreachable container is still inside
    its grace window — the honest "not confirmed either way" answer, not a
    reason to act.
    """
    if state.container_status in _STARTABLE:
        return "start"
    if state.container_status == "running" and not state.reachable:
        # Strictly greater: the constant counts polls *tolerated*, so the
        # action lands on the poll after the window, not on its last tick.
        return "restart" if wd.unreachable_ticks > UNREACHABLE_GRACE_TICKS else None
    # `unknown`, or any status Docker grows later: `up -d` is the safe,
    # idempotent guess, and the backoff bounds how often we guess.
    return "start"


async def _tick(wd: _WatchdogState) -> None:
    """One supervision poll. Isolates its own failures — never raises."""
    try:
        state = await asyncio.to_thread(fetch_searxng_state)
    except Exception as exc:  # noqa: BLE001 — background task must survive
        message = f"status read failed: {exc}"
        if message != wd.last_error:
            logger.warning("⚠️  SearXNG watchdog could not read status: %s", exc)
            wd.last_error = message
        return

    if state.available:
        _note_healthy(wd)
        return

    # Track how long an up-but-silent container has been silent. This is an
    # observation, so it advances even while the backoff is suppressing
    # attempts — otherwise a backed-off container could never age out of its
    # grace window.
    if state.container_status == "running" and not state.reachable:
        wd.unreachable_ticks += 1
    else:
        wd.unreachable_ticks = 0

    message = _describe(state)
    if message != wd.last_error:
        logger.warning("⚠️  SearXNG unavailable — voice web search is degraded: %s", message)
        wd.last_error = message

    if wd.backoff.seconds_remaining() is not None:
        return

    action = _plan(state, wd)
    if action is None:
        return

    await _attempt(action, wd)


async def _attempt(action: str, wd: _WatchdogState) -> None:
    """Run one recovery action and record whether it actually restored service."""
    recover = start_searxng if action == "start" else restart_searxng
    logger.info("ℹ️  SearXNG watchdog attempting %s…", action)
    try:
        state = await asyncio.to_thread(recover)
    except SearxngConfigError as exc:
        # Config vanished under a running webapp; nothing to do but stay quiet.
        if str(exc) != wd.last_error:
            logger.warning("⚠️  SearXNG watchdog cannot recover: %s", exc)
            wd.last_error = str(exc)
        wd.backoff.record_failure()
        return
    except SearxngCommandError as exc:
        delay = wd.backoff.record_failure()
        logger.warning("⚠️  SearXNG %s failed (retry in %.0fs): %s", action, delay, exc)
        return
    except Exception as exc:  # noqa: BLE001 — background task must survive
        delay = wd.backoff.record_failure()
        logger.warning("⚠️  SearXNG %s errored (retry in %.0fs): %s", action, delay, exc)
        return

    if state.available:
        _note_healthy(wd, recovered_by=action)
        return

    # The command succeeded but the service is still not answering — a
    # `docker compose up` that exits 0 is not the same fact as "search works",
    # so it must not be recorded as a success.
    delay = wd.backoff.record_failure()
    logger.warning(
        "⚠️  SearXNG %s ran but service is still unavailable (retry in %.0fs): %s",
        action,
        delay,
        _describe(state),
    )


def _note_healthy(wd: _WatchdogState, *, recovered_by: Optional[str] = None) -> None:
    """Record a confirmed-healthy read, logging only on the transition into it."""
    if wd.last_error is not None:
        if recovered_by:
            logger.info("✅ SearXNG recovered after %s — voice web search is live again", recovered_by)
        else:
            logger.info("✅ SearXNG recovered — voice web search is live again")
    wd.last_error = None
    wd.unreachable_ticks = 0
    wd.backoff.record_success()


def start_searxng_watchdog() -> Optional[asyncio.Task]:
    """Start supervision when the stack path is configured; otherwise no-op.

    An unset ``SEARXNG_COMPOSE_PATH`` means this host does not own the SearXNG
    stack, so there is nothing to supervise — the webapp is left entirely
    unaffected, matching ``start_ha_trace_collector``'s shape.
    """
    try:
        compose_path()
    except SearxngConfigError as exc:
        logger.info("ℹ️  SearXNG watchdog not started: %s", exc)
        return None

    wd = _WatchdogState()
    return asyncio.create_task(
        run_loop(
            lambda: _tick(wd),
            POLL_SECONDS,
            logger=logger,
            name="SearXNG watchdog",
            start_msg="🔎 SearXNG watchdog started (poll %ds)" % POLL_SECONDS,
        )
    )
