"""Background Tesla sampler — owned by the webapp lifecycle, read-only.

One asyncio task started in the FastAPI lifespan, only where the automation
engines are on and this process holds the ownership lock, and only when
``TESLA_SAMPLER_ENABLED=1`` (default off: every read is a billed Fleet API
request, and the car must never be polled from a dev, worktree or test boot).

The cadence is built around not keeping the car awake. Every read first asks
for the car's connectivity state, which does not wake it; ``vehicle_data`` is
fetched only while it is online. Then:

* driving or charging — read every ``TESLA_ACTIVE_INTERVAL_S`` (180 s),
  straight to ``vehicle_data`` with no separate state check;
* online but idle — every ``TESLA_IDLE_INTERVAL_S`` (900 s), and after
  ``TESLA_IDLE_BEFORE_QUIET_S`` (900 s) of idling, stop reading data for
  ``TESLA_QUIET_S`` (1200 s) so the car can fall asleep (data reads reset its
  sleep timer; state checks do not);
* asleep or offline — a state check every ``TESLA_IDLE_INTERVAL_S``.

Every Fleet API request is billed (Data: 500 requests per $1, with a $10
monthly account discount), so the defaults are sized to stay inside the
discount — the budget is worked through in ``docs/tesla.md`` — and every read
logs its state and today's request count at info level. The blocking SQLite work runs via :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from app.webapp._task_loop import run_loop
from src._env import _env_bool, _env_int
from src.devices import tesla_client, tesla_history

logger = logging.getLogger(__name__)

# The loop wakes this often to decide whether a read is due; the read
# cadence itself comes from :class:`TeslaSamplerConfig`.
_LOOP_TICK_S = 30
_PRUNE_INTERVAL_S = 24 * 3600


@dataclass(frozen=True)
class TeslaSamplerConfig:
    enabled: bool = False
    active_interval_s: int = 180
    idle_interval_s: int = 900
    idle_before_quiet_s: int = 900
    quiet_s: int = 1200


def load_sampler_config() -> TeslaSamplerConfig:
    load_dotenv(override=True)
    return TeslaSamplerConfig(
        enabled=_env_bool("TESLA_SAMPLER_ENABLED", False),
        active_interval_s=max(60, _env_int("TESLA_ACTIVE_INTERVAL_S", 180)),
        idle_interval_s=max(60, _env_int("TESLA_IDLE_INTERVAL_S", 900)),
        idle_before_quiet_s=max(60, _env_int("TESLA_IDLE_BEFORE_QUIET_S", 900)),
        quiet_s=max(60, _env_int("TESLA_QUIET_S", 1200)),
    )


@dataclass
class CadenceState:
    """In-memory timing (monotonic seconds); not persisted."""

    last_read: Optional[float] = None
    active: bool = False
    # When the current online-but-idle stretch began; None while active/asleep.
    idle_since: Optional[float] = None
    quiet_until: float = 0.0
    last_prune: float = 0.0


def read_due(state: CadenceState, config: TeslaSamplerConfig, now: float) -> bool:
    """Whether a read is due now (pure, so the cadence is unit-testable)."""
    if state.last_read is None:
        return True
    interval = config.active_interval_s if state.active else config.idle_interval_s
    return now - state.last_read >= interval


def data_allowed(state: CadenceState, now: float) -> bool:
    """False inside the quiet window that lets an idle car fall asleep."""
    return now >= state.quiet_until


def note_reading(state: CadenceState, config: TeslaSamplerConfig, reading: tesla_client.TeslaReading,
                 now: float) -> None:
    """Advance the cadence from a reading (pure apart from ``state``)."""
    state.last_read = now
    sample = reading.sample
    if sample is not None and (sample.driving or sample.charging):
        state.active, state.idle_since, state.quiet_until = True, None, 0.0
        return
    state.active = False
    if not reading.awake:
        state.idle_since = None
        return
    if sample is None:
        return  # a state-only check inside the quiet window: the idle clock is paused
    if state.idle_since is None:
        state.idle_since = now
    elif now - state.idle_since >= config.idle_before_quiet_s:
        state.quiet_until = now + config.quiet_s
        state.idle_since = None
        logger.info("🚗 Tesla idle %d s — pausing data reads %d s so it can sleep",
                    config.idle_before_quiet_s, config.quiet_s)


async def _tick(config: TeslaSamplerConfig, history: tesla_history.TeslaHistoryConfig,
                state: CadenceState) -> None:
    now = time.monotonic()
    if read_due(state, config, now):
        reading = await tesla_client.read_vehicle(
            with_data=data_allowed(state, now), known_awake=state.active
        )
        note_reading(state, config, reading, now)
        logger.info("🚗 Tesla %s (requests today: %d)", reading.state, tesla_client.REQUESTS.count)
        try:
            await asyncio.to_thread(tesla_history.record_status, reading.state, reading.error)
            if reading.sample is not None:
                await asyncio.to_thread(tesla_history.record_sample, reading.sample)
                await asyncio.to_thread(tesla_history.refresh_sessions, history)
        except Exception as exc:  # noqa: BLE001 — a store failure never kills the loop
            logger.warning("⚠️ Tesla history write failed: %s", exc)
    if now - state.last_prune >= _PRUNE_INTERVAL_S:
        state.last_prune = now
        try:
            await asyncio.to_thread(tesla_history.prune, history)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Tesla history prune failed: %s", exc)


async def _run(config: TeslaSamplerConfig) -> None:
    history = tesla_history.load_history_config()
    await asyncio.to_thread(tesla_history.init_db)
    state = CadenceState()
    await run_loop(
        lambda: _tick(config, history, state),
        _LOOP_TICK_S,
        logger=logger,
        name="Tesla sampler",
        start_msg=(
            "🚗 Tesla sampler started (active %ds, idle %ds, quiet %ds after %ds idle)"
            % (config.active_interval_s, config.idle_interval_s, config.quiet_s, config.idle_before_quiet_s)
        ),
    )


def start_tesla_sampler() -> asyncio.Task | None:
    """Start the sampler task if enabled; return it (or ``None`` when disabled)."""
    config = load_sampler_config()
    try:
        tesla_history.init_db()
    except Exception as exc:  # noqa: BLE001 — the API still answers without history
        logger.warning("⚠️ Could not init Tesla history DB: %s", exc)
    if not config.enabled:
        logger.info("ℹ️ Tesla sampler disabled (TESLA_SAMPLER_ENABLED) — not reading the car")
        return None
    return asyncio.create_task(_run(config), name="tesla-sampler")
