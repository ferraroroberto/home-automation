"""Opt-in nightly speed test — owned by the webapp (uvicorn) lifecycle (#840).

One asyncio task started in the FastAPI lifespan by the automation owner, like
the other write-side loops (no separate daemon). Every minute it asks one
question — *is tonight's test due?* — and, only when the user has switched the
nightly test on (:mod:`src.speedtest_prefs`, **off by default**), runs the same
host-side test the tile's button runs and stores the result in the history the
sparklines read.

Due means: local time is inside the catch-up window that opens at
``NIGHTLY_HOUR`` (so an app that was down at the quiet hour still tests a
little later, never at midday) **and** the store holds no speed result since
that hour. The store is the memory, so a restart neither loses nor repeats
tonight's test. A test that yields nothing (offline, speedtest-cli missing) is
retried at most ``MAX_ATTEMPTS`` times a night, ``RETRY_GAP_S`` apart, so a dead
link can't turn this into a bandwidth loop.

Gated by ``SPEEDTEST_SCHEDULE_ENABLED`` (``.env``, default on) so the e2e suite
and dev runs never start it; the user-facing opt-in is the pref, not this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from app.webapp._env import _env_bool
from app.webapp._task_loop import run_loop
from src.network_history import last_speed_sample_ts, record_internet_sample
from src.network_host import fetch_internet_health
from src.speedtest_prefs import NIGHTLY_HOUR, load_speedtest_prefs

logger = logging.getLogger(__name__)

TICK_S = 60.0
CATCH_UP_H = 3          # window after NIGHTLY_HOUR in which a missed test still runs
MAX_ATTEMPTS = 3        # per night, when the test yields nothing
RETRY_GAP_S = 30 * 60
TEST_TIMEOUT_S = 180.0  # the test takes ~13 s; this only bounds a wedge


@dataclass
class _NightState:
    """In-memory retry bookkeeping (not persisted — the store holds the outcome)."""

    day: Optional[date] = None
    attempts: int = 0
    last_attempt: float = 0.0


def is_due(now: datetime, last_speed_ts: Optional[int]) -> bool:
    """Whether tonight's test should run: inside the window and not yet done."""
    start = now.replace(hour=NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
    if not start <= now < start + timedelta(hours=CATCH_UP_H):
        return False
    return last_speed_ts is None or last_speed_ts < int(start.timestamp())


async def _run_test(now: datetime) -> bool:
    """Run one speed test and store it; ``True`` when it produced a result."""
    health = await asyncio.wait_for(
        fetch_internet_health(include_speedtest=True), timeout=TEST_TIMEOUT_S
    )
    sample = {
        "online": health.online,
        "external_ms": health.external_ms,
        "gateway_ms": health.gateway_ms,
        "packet_loss_pct": health.packet_loss_pct,
        "download_mbps": health.download_mbps,
        "upload_mbps": health.upload_mbps,
    }
    await asyncio.to_thread(record_internet_sample, sample, int(now.timestamp()))
    if health.download_mbps is None and health.upload_mbps is None:
        return False
    logger.info(
        "✅ Nightly speed test: %.0f Mbps down, %.0f Mbps up",
        health.download_mbps or 0.0,
        health.upload_mbps or 0.0,
    )
    return True


async def _tick(state: _NightState, now: Optional[datetime] = None) -> None:
    if not load_speedtest_prefs().nightly_enabled:
        return
    now = now or datetime.now()
    last_ts = await asyncio.to_thread(last_speed_sample_ts)
    if not is_due(now, last_ts):
        return
    if state.day != now.date():
        state.day, state.attempts, state.last_attempt = now.date(), 0, 0.0
    if state.attempts >= MAX_ATTEMPTS:
        return
    if state.attempts and time.monotonic() - state.last_attempt < RETRY_GAP_S:
        return
    state.attempts += 1
    state.last_attempt = time.monotonic()
    try:
        ok = await _run_test(now)
    except Exception as exc:  # noqa: BLE001 — a failed test never kills the loop
        logger.warning("⚠️ Nightly speed test failed (attempt %d/%d): %s", state.attempts, MAX_ATTEMPTS, exc)
        return
    if not ok:
        logger.warning(
            "⚠️ Nightly speed test produced no result (attempt %d/%d)", state.attempts, MAX_ATTEMPTS
        )


def start_nightly_speedtest() -> Optional[asyncio.Task]:
    """Start the nightly speed-test task unless the process-level gate is off."""
    if not _env_bool("SPEEDTEST_SCHEDULE_ENABLED", True):
        logger.info("ℹ️ Nightly speed-test scheduler disabled (SPEEDTEST_SCHEDULE_ENABLED)")
        return None
    state = _NightState()

    async def tick() -> None:
        await _tick(state)

    return asyncio.create_task(
        run_loop(
            tick,
            TICK_S,
            logger=logger,
            name="nightly-speedtest",
            start_msg=(
                "🚀 Nightly speed-test scheduler started "
                f"(opt-in, due from {NIGHTLY_HOUR:02d}:00)"
            ),
            tick_fail_msg="⚠️ Nightly speed-test tick failed: %s",
        ),
        name="nightly-speedtest",
    )
