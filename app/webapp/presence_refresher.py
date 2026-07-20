"""Bounded iCloud Find My refresher for Presence diagnostics.

Browser polling reads this module's in-memory cache through ``GET
/api/presence``. The expensive Apple call happens only in this background task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src.presence_client import (
    PresenceAuthError,
    PresenceConfig,
    PresenceConfigError,
    PresenceEntity,
    fetch_presence,
    load_presence_configs,
)

logger = logging.getLogger(__name__)


@dataclass
class PresenceAccountStatus:
    """Per-account outcome of the last Find My refresh (issue #478)."""

    label: str
    available: bool
    reason: str
    detail: str = ""
    entity_count: int = 0


@dataclass
class PresenceDiagnosticsCache:
    """Latest cached Find My diagnostic snapshot."""

    entities: list[PresenceEntity]
    refreshed_at: Optional[datetime] = None
    available: bool = False
    reason: str = "not_refreshed"
    detail: str = ""
    home_radius_m: Optional[float] = None
    accounts: list[PresenceAccountStatus] = field(default_factory=list)


_CACHE = PresenceDiagnosticsCache(entities=[])


def _aggregate_status(
    statuses: list[PresenceAccountStatus],
) -> tuple[str, str]:
    """Roll per-account outcomes into a single ``(reason, detail)`` (issue #478).

    - all healthy → ``ok``
    - single account → its own reason/detail verbatim (preserves the pre-#478
      single-account contract the router speech + PWA note key off)
    - some healthy, some broken → ``partial`` (the cache still carries the
      healthy accounts' entities, so the source is not "down")
    - every account broken → the dominant failure reason, worst-first
    """

    failed = [s for s in statuses if not s.available]
    if not failed:
        return "ok", ""
    if len(statuses) == 1:
        return failed[0].reason, failed[0].detail

    combined = "; ".join(
        f"account {s.label} [{s.reason}] {s.detail}".strip() for s in failed
    )
    if len(failed) < len(statuses):
        broken = ", ".join(s.label for s in failed)
        return (
            "partial",
            f"{len(failed)} of {len(statuses)} iCloud accounts need re-auth "
            f"(account {broken}): {combined}",
        )
    for reason in ("2fa_required", "error", "not_configured"):
        if any(s.reason == reason for s in failed):
            return reason, combined
    return "error", combined


def get_cache() -> PresenceDiagnosticsCache:
    """Return the current diagnostics cache."""

    return _CACHE


def _fetch_account(config: PresenceConfig) -> tuple[list[PresenceEntity], PresenceAccountStatus]:
    """Fetch one account's Find My devices, mapping failures to a per-account status.

    A failure here degrades only this account — the caller keeps every other
    account's entities (issue #478), so one Apple ID needing 2FA never blanks the
    whole snapshot.
    """

    try:
        entities = fetch_presence(config=config)
    except PresenceAuthError as exc:
        return [], PresenceAccountStatus(config.label, False, "2fa_required", str(exc))
    except PresenceConfigError as exc:
        return [], PresenceAccountStatus(config.label, False, "not_configured", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ Failed to refresh iCloud account %s: %s", config.label, exc
        )
        return [], PresenceAccountStatus(config.label, False, "error", str(exc))
    return entities, PresenceAccountStatus(
        config.label, True, "ok", "", entity_count=len(entities)
    )


async def refresh_once() -> PresenceDiagnosticsCache:
    """Fetch every configured account's Find My devices once into the cache.

    Each account authenticates independently and degrades independently: a
    healthy account still populates the cache when another needs 2FA (#478).
    Accounts are fetched concurrently, not sequentially (#491) — a caller
    bounding this coroutine with a single overall timeout (the on-demand
    locate refresh in ``routers/presence.py``) would otherwise have that
    budget split serially across accounts, making a 2-account setup roughly
    twice as likely to lose the race as a 1-account one.
    """

    global _CACHE
    now = datetime.now(timezone.utc)
    try:
        configs = load_presence_configs()
    except PresenceConfigError as exc:
        _CACHE = PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=now,
            available=False,
            reason="not_configured",
            detail=str(exc),
        )
        return _CACHE

    results = await asyncio.gather(
        *(asyncio.to_thread(_fetch_account, config) for config in configs)
    )
    entities: list[PresenceEntity] = []
    statuses: list[PresenceAccountStatus] = []
    for got, status in results:
        entities.extend(got)
        statuses.append(status)

    reason, detail = _aggregate_status(statuses)
    _CACHE = PresenceDiagnosticsCache(
        entities=entities,
        refreshed_at=now,
        available=any(s.available for s in statuses),
        reason=reason,
        detail=detail,
        home_radius_m=configs[0].home_radius_m,
        accounts=statuses,
    )
    return _CACHE


async def _run(interval_s: int) -> None:
    await run_loop(
        refresh_once,
        interval_s,
        logger=logger,
        name="Presence diagnostics refresher",
        start_msg="📍 Presence diagnostics refresher started (interval %ds)" % interval_s,
    )


def start_presence_refresher() -> Optional[asyncio.Task]:
    """Start the bounded iCloud refresher unless disabled."""

    load_dotenv(override=True)
    if not _env_bool("PRESENCE_ICLOUD_REFRESH_ENABLED", True):
        logger.info("ℹ️ Presence diagnostics refresher disabled")
        return None
    interval_s = max(60, _env_int("PRESENCE_ICLOUD_REFRESH_INTERVAL_S", 900))
    return asyncio.create_task(_run(interval_s), name="presence-refresher")
