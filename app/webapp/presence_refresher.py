"""Bounded iCloud Find My refresher for Presence diagnostics.

Browser polling reads this module's in-memory cache through ``GET
/api/presence``. The expensive Apple call happens only in this background task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from dotenv import load_dotenv

from app.webapp._env import _env_bool, _env_int
from app.webapp._task_loop import run_loop
from src.notify import Notifier, NotifierError
from src.notify_config import build_alarm_notifier
from src.presence_client import (
    PresenceAuthError,
    PresenceConfig,
    PresenceConfigError,
    PresenceEntity,
    fetch_presence,
    invalidate_session,
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


_RETRY_BACKOFF_S_DEFAULT = 4 * 60 * 60  # 4 hours


def _retry_backoff_s() -> int:
    return max(60, _env_int("PRESENCE_ICLOUD_RETRY_BACKOFF_S", _RETRY_BACKOFF_S_DEFAULT))


# Per-account (keyed by ``PresenceConfig.label``) timestamp of the last forced
# session rebuild attempted while that account was broken (issue #655). A
# healthy account never touches this — only the backoff-gated self-heal retry
# below does. In-memory only: a tray restart already forces a fresh attempt via
# ``presence_client._connect()``'s normal cache-miss path, so there is nothing
# useful to persist across restarts.
_LAST_RETRY_ATTEMPT: Dict[str, datetime] = {}


def _retry_due(label: str, *, now: datetime) -> bool:
    last = _LAST_RETRY_ATTEMPT.get(label)
    return last is None or (now - last).total_seconds() >= _retry_backoff_s()


def _account_display_name(config: PresenceConfig) -> str:
    """Name an account in a Telegram message — friendly name if set, else the
    Apple ID email itself (issue #658: "account 1"/"account 2" gave no way to
    tell which real account a reconnect message was actually about)."""

    return config.friendly_name or config.email


def _notify(notifier_factory: Callable[[], Optional[Notifier]], text: str) -> None:
    """Best-effort Telegram send — never lets a delivery failure break a poll."""

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(text)
    except NotifierError as exc:
        logger.warning("⚠️ Telegram presence notification failed: %s", exc)


def _fetch_account(
    config: PresenceConfig,
    *,
    prev_status: Optional[PresenceAccountStatus],
    notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier,
) -> tuple[list[PresenceEntity], PresenceAccountStatus]:
    """Fetch one account's Find My devices, mapping failures to a per-account status.

    A failure here degrades only this account — the caller keeps every other
    account's entities (issue #478), so one Apple ID needing 2FA never blanks the
    whole snapshot.

    Issue #655: when ``prev_status`` shows this account was already broken, a
    fresh sign-in handshake is forced (and Telegram-announced beforehand) at
    most once per :func:`_retry_backoff_s` — self-healing a session that is
    actually fine again (the trusted cookies were re-validated out of band, or
    Apple's own hold lifted) without needing a manual tray restart, while
    staying far short of #651's every-poll re-authentication that was
    triggering repeated "someone is trying to access your account" prompts. A
    healthy account is never touched by this — same caching/cadence as before.

    Issue #656: the backoff timestamp is deliberately *not* cleared once the
    account recovers (see the bottom of this function). pyicloud's FindMy
    sub-service does its own internal re-authentication when its own token
    times out, and when that needs 2FA it swallows the failure rather than
    raising — leaving the cached session's in-memory ``requires_2fa`` stuck
    true even though nothing actually needs a human. A "healthy-looking"
    account can flip broken again within minutes; clearing the timestamp on
    every recovery made each such flap look like a brand-new failure to
    :func:`_retry_due`, so the backoff never actually throttled anything and
    every flap forced a fresh Apple handshake roughly every 30 minutes.
    """

    now = datetime.now(timezone.utc)
    was_broken = prev_status is not None and not prev_status.available
    if was_broken and _retry_due(config.label, now=now):
        invalidate_session(config)
        _LAST_RETRY_ATTEMPT[config.label] = now
        _notify(
            notifier_factory,
            f"🔑 Reconnecting {_account_display_name(config)}'s iCloud Find My — "
            "approve the sign-in on a trusted device if prompted.",
        )

    try:
        entities = fetch_presence(config=config)
    except PresenceAuthError as exc:
        entities = []
        status = PresenceAccountStatus(config.label, False, "2fa_required", str(exc))
    except PresenceConfigError as exc:
        entities = []
        status = PresenceAccountStatus(config.label, False, "not_configured", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ Failed to refresh iCloud account %s: %s", config.label, exc
        )
        entities = []
        status = PresenceAccountStatus(config.label, False, "error", str(exc))
    else:
        status = PresenceAccountStatus(
            config.label, True, "ok", "", entity_count=len(entities)
        )

    if was_broken and status.available:
        # Deliberately not popped from _LAST_RETRY_ATTEMPT (issue #656) — see
        # this function's docstring. The backoff clock keeps running from the
        # last forced handshake regardless of a recovery in between, so a
        # flapping account still gets throttled to one handshake per window.
        _notify(
            notifier_factory,
            f"✅ {_account_display_name(config)}'s iCloud Find My connection restored.",
        )

    return entities, status


async def refresh_once(
    *, notifier_factory: Callable[[], Optional[Notifier]] = build_alarm_notifier
) -> PresenceDiagnosticsCache:
    """Fetch every configured account's Find My devices once into the cache.

    Each account authenticates independently and degrades independently: a
    healthy account still populates the cache when another needs 2FA (#478).
    Accounts are fetched concurrently, not sequentially (#491) — a caller
    bounding this coroutine with a single overall timeout (the on-demand
    locate refresh in ``routers/presence.py``) would otherwise have that
    budget split serially across accounts, making a 2-account setup roughly
    twice as likely to lose the race as a 1-account one.

    ``notifier_factory`` is an injection seam for tests (issue #655) — every
    real caller uses the default, which is hard-disabled under pytest anyway
    (see ``build_alarm_notifier``'s safety net).
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

    prev_by_label = {status.label: status for status in _CACHE.accounts}
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                _fetch_account,
                config,
                prev_status=prev_by_label.get(config.label),
                notifier_factory=notifier_factory,
            )
            for config in configs
        )
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
