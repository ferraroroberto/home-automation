"""Background energy sampler — owned by the webapp (uvicorn) lifecycle.

Runs as a single asyncio task started in the FastAPI lifespan, so it lives and
dies with the webapp process the tray (or ``webapp.bat``) owns — no separate
daemon. Every ``persist_interval_s`` it reads the live solar flow and persists one
sample; every ``compact_interval_s`` it folds completed hours into rollups and
prunes old raw data (see :mod:`src.energy_history`).

Gated by ``ENERGY_SAMPLER_ENABLED`` (``.env``): disabled, the webapp serves the
live snapshot + whatever history already exists but writes nothing — which is
how the e2e suite and dev runs avoid hammering the real FusionSolar cloud.

The blocking SQLite writes run via :func:`asyncio.to_thread` so the read part
of the loop never stalls the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.webapp._task_loop import run_loop
from src.energy_history import (
    EnergyHistoryConfig,
    compact_and_prune,
    init_db,
    load_history_config,
    record_sample,
)
from src.huawei_client import fetch_energy_day, fetch_energy_state

logger = logging.getLogger(__name__)


@dataclass
class _SamplerState:
    """In-memory timing state (not persisted): monotonic ts of the last compact."""

    last_compact: float = 0.0


async def _tick(config: EnergyHistoryConfig, state: _SamplerState) -> None:
    try:
        sample = await fetch_energy_state()
        await asyncio.to_thread(record_sample, sample)
    except Exception as exc:  # noqa: BLE001 — never let a read kill the loop
        logger.warning("⚠️ Energy sample failed: %s", exc)

    now = time.monotonic()
    if now - state.last_compact >= config.compact_interval_s:
        try:
            await asyncio.to_thread(compact_and_prune, config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Energy compaction failed: %s", exc)
        state.last_compact = now


async def _backfill_today() -> None:
    """Replay today's cloud series into the history, filling any gap.

    The charts and kWh cards integrate *persisted samples*, so any stretch the
    sampler did not cover — an overnight restart, or the changeover to this
    client — would otherwise stay empty for good. The portal already returns
    the whole day on every read, so this costs no extra API call, and the
    writes are timestamp-keyed and idempotent.

    Best-effort by design: a failure here must not stop live sampling.
    """
    try:
        rows = await fetch_energy_day()
    except Exception as exc:  # noqa: BLE001 — never block the sampler
        logger.warning("⚠️ Energy backfill read failed: %s", exc)
        return
    if not rows:
        return

    def _write() -> None:
        for ts, sample in rows:
            record_sample(sample, ts=ts)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Energy backfill write failed: %s", exc)
        return
    logger.info("📈 Backfilled %d energy samples from today's cloud series", len(rows))


async def _run(config: EnergyHistoryConfig) -> None:
    """Sample → persist → periodically compact, until cancelled."""
    await asyncio.to_thread(init_db)
    await _backfill_today()
    state = _SamplerState()
    await run_loop(
        lambda: _tick(config, state),
        config.persist_interval_s,
        logger=logger,
        name="Energy sampler",
        start_msg=(
            "📈 Energy sampler started (persist %ds, compact %ds, raw retention %dd)"
            % (config.persist_interval_s, config.compact_interval_s, config.raw_retention_days)
        ),
    )


def start_sampler() -> asyncio.Task | None:
    """Start the sampler task if enabled; return it (or ``None`` when disabled)."""
    config = load_history_config()
    if not config.enabled:
        logger.info("ℹ️ Energy sampler disabled (ENERGY_SAMPLER_ENABLED) — not persisting")
        # Still ensure the schema exists so the history API answers cleanly.
        try:
            init_db()
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Could not init energy-history DB: %s", exc)
        return None
    return asyncio.create_task(_run(config), name="energy-sampler")
