"""Bounded iCloud Find My refresher for Presence diagnostics.

Browser polling reads this module's in-memory cache through ``GET
/api/presence``. The expensive Apple call happens only in this background task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
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
    session_trust_state,
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
    # Issue #659: who this account is (friendly name or Apple ID email) and
    # whether its cached session still holds Apple's ~30-day browser trust —
    # ``None`` until a session has been built. Untrusted is *not* unavailable:
    # Find My keeps serving, only fresh sign-ins get expensive (#658).
    display_name: str = ""
    trusted: Optional[bool] = None
    # Issue #678: how many consecutive refreshes have failed for this account,
    # carried across polls via ``_CACHE.accounts``. 0 whenever the last fetch
    # succeeded. Both the self-heal handshake and the Telegram alert are gated
    # on this streak, so a transient Apple hiccup costs neither.
    consecutive_failures: int = 0
    # When the current run of failures started (ISO-8601 UTC), ``None`` while
    # healthy. Measured, not inferred: ``refresh_once`` is also driven on demand
    # by the locate path, so streak x poll-interval would overstate the age of a
    # break the user happened to hammer. ISO strings rather than ``datetime`` to
    # match how the router already emits ``refreshed_at``.
    failing_since: Optional[str] = None


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
_REFRESH_INTERVAL_S_DEFAULT = 15 * 60  # 15 minutes

# Issue #678: how many consecutive failed refreshes an account must rack up
# before it is treated as genuinely broken rather than as an Apple hiccup.
#
# ``_SELF_HEAL`` gates the forced sign-in handshake: Apple answers the Find My
# fetch with a transient 409 ("Authentication required for Account.") often
# enough that reacting to a single one threw away a working session for
# nothing. ``_ALERT`` gates the Telegram message, and sits deliberately well
# past the point the self-heal above could still have worked — a streak that
# long is a stuck account (changed password, lapsed trust, Apple lock), never a
# blip. At the default 15-minute poll the 2nd failure lands ~15 min into a break
# and the 4th ~45 min in, so nothing is said for the first three quarters of an
# hour — long past any hiccup this has ever been observed to be.
_SELF_HEAL_AFTER_FAILURES_DEFAULT = 2
_ALERT_AFTER_FAILURES_DEFAULT = 4


def _retry_backoff_s() -> int:
    return max(60, _env_int("PRESENCE_ICLOUD_RETRY_BACKOFF_S", _RETRY_BACKOFF_S_DEFAULT))


def _refresh_interval_s() -> int:
    return max(
        60, _env_int("PRESENCE_ICLOUD_REFRESH_INTERVAL_S", _REFRESH_INTERVAL_S_DEFAULT)
    )


def _self_heal_after_failures() -> int:
    return max(
        1,
        _env_int(
            "PRESENCE_ICLOUD_SELF_HEAL_AFTER_FAILURES",
            _SELF_HEAL_AFTER_FAILURES_DEFAULT,
        ),
    )


def _alert_after_failures() -> int:
    return max(
        1,
        _env_int("PRESENCE_ICLOUD_ALERT_AFTER_FAILURES", _ALERT_AFTER_FAILURES_DEFAULT),
    )


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


# Per-account (keyed by ``PresenceConfig.label``) latch for the "this account is
# stuck" Telegram alert (issue #678). One message per broken episode, not one
# per poll; cleared the moment the account fetches successfully again, which is
# also the only thing that unlocks the paired recovery message. In-memory only,
# same rationale as ``_LAST_RETRY_ATTEMPT``.
_ALERTED: set[str] = set()


def _humanize_duration(seconds: float) -> str:
    """Render an elapsed span as "45m" / "1h" / "2h 30m"."""

    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


def _episode_start(status: PresenceAccountStatus) -> Optional[datetime]:
    """Parse the current break's start stamp, ``None`` if there isn't a usable one."""

    if not status.failing_since:
        return None
    try:
        return datetime.fromisoformat(status.failing_since)
    except ValueError:
        return None


def _failing_for(status: PresenceAccountStatus, *, now: datetime) -> str:
    """How long this account's current break has actually lasted (#678).

    Falls back to the streak count alone when the episode has no start stamp —
    an unestablished duration is not worth stating as one.
    """

    started = _episode_start(status)
    if started is None:
        return ""
    return _humanize_duration((now - started).total_seconds())


def _retried_this_episode(label: str, status: PresenceAccountStatus) -> bool:
    """Whether a forced sign-in was actually attempted during this break (#678).

    Not the same question as "has this account been failing long enough" — the
    handshake is throttled to one per :func:`_retry_backoff_s` (4h), far longer
    than the alert threshold, so an episode that opens shortly after a previous
    forced retry never gets one of its own. Telling the user "a fresh sign-in
    did not fix it" in that case would state something that never happened.
    """

    last = _LAST_RETRY_ATTEMPT.get(label)
    started = _episode_start(status)
    return last is not None and started is not None and last >= started


def _stuck_alert_text(
    config: PresenceConfig, status: PresenceAccountStatus, *, now: datetime
) -> str:
    """Compose the one actionable message a genuinely stuck account earns (#678).

    Deliberately says nothing about approving a sign-in: since #658 the
    unattended refresher fetches with ``request_2fa_push=False``, so it can
    never make Apple push a prompt to the household's phones. What it names
    instead is the remedy that does exist — the in-app trust renewal (#659) or
    the credential the account is missing.
    """

    detail = " ".join(status.detail.split())[:200]
    reason = f"[{status.reason}] {detail}".strip()
    age = _failing_for(status, now=now)
    span = f"for ~{age} " if age else ""
    if status.reason == "not_configured":
        remedy = (
            "Set this account's ICLOUD_EMAIL/ICLOUD_PASSWORD in .env and restart the tray."
        )
    else:
        tried = (
            "A fresh sign-in did not fix it. "
            if _retried_this_episode(config.label, status)
            else ""
        )
        remedy = (
            f"{tried}Open Presence → Renew trust for this account, or update its "
            "ICLOUD_PASSWORD in .env if the Apple ID password changed."
        )
    return (
        f"⚠️ {_account_display_name(config)}'s iCloud Find My has been failing "
        f"{span}({status.consecutive_failures} consecutive refreshes) — {reason}\n"
        f"{remedy}"
    )


def _account_display_name(config: PresenceConfig) -> str:
    """Name an account in a Telegram message — friendly name if set, else the
    Apple ID email itself (issue #657: "account 1"/"account 2" gave no way to
    tell which real account a reconnect message was actually about). Same rule
    the diagnostics rows use (#659), so the message and the row agree."""

    return config.display_name


def _notify(notifier_factory: Callable[[], Optional[Notifier]], text: str) -> None:
    """Best-effort Telegram send — never lets a delivery failure break a poll."""

    notifier = notifier_factory()
    if notifier is None:
        return
    try:
        notifier.send_text(text)
    except NotifierError as exc:
        logger.warning("⚠️ Telegram presence notification failed: %s", exc)


def _account_status(
    config: PresenceConfig,
    available: bool,
    reason: str,
    detail: str = "",
    *,
    entity_count: int = 0,
    consecutive_failures: int = 0,
    failing_since: Optional[str] = None,
) -> PresenceAccountStatus:
    """Build one account's status, stamped with its name + session trust (#659)."""

    return PresenceAccountStatus(
        config.label,
        available,
        reason,
        detail,
        entity_count=entity_count,
        display_name=_account_display_name(config),
        trusted=session_trust_state(config),
        consecutive_failures=consecutive_failures,
        failing_since=failing_since,
    )


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

    Issue #655: when this account has been failing, a fresh sign-in handshake is
    forced at most once per :func:`_retry_backoff_s` — self-healing a session
    that is actually fine again (the trusted cookies were re-validated out of
    band, or Apple's own hold lifted) without needing a manual tray restart,
    while staying far short of #651's every-poll re-authentication that was
    triggering repeated "someone is trying to access your account" prompts. A
    healthy account is never touched by this — same caching/cadence as before.

    Issue #656: the backoff timestamp is deliberately *not* cleared once the
    account recovers (see the bottom of this function) — the backoff clock
    runs from the last forced handshake, so an account that keeps failing
    and recovering is still throttled to one handshake per window.

    Issue #658: "broken" means the Find My fetch itself failed. pyicloud's
    FindMy sub-service re-authenticates internally when Apple answers 450 and,
    when the browser trust has expired, leaves the cached session's in-memory
    ``requires_2fa`` true while still serving every device;
    :func:`fetch_presence` no longer treats that flag as a failure, so a
    working session is never reported ``2fa_required`` here and this
    self-heal fires only for real breakage. The fetch runs with
    ``request_2fa_push=False``: this process can never enter a 2FA code, so
    pyicloud must not ask Apple to push one to the household's phones on every
    fresh sign-in.

    Issue #678: one failed poll is not breakage. Apple answers the Find My
    fetch with a transient 409 ("Authentication required for Account.") often
    enough that reacting to a single one threw away a working, *trusted*
    session and woke the household with a Telegram pair for something that
    healed itself two polls later. Both reactions are now gated on a
    consecutive-failure streak, and the handshake no longer announces itself:
    the pre-emptive "approve the sign-in if prompted" heads-up existed to
    explain an Apple push that, since #658's ``request_2fa_push=False``, this
    process can no longer cause. What is left is one actionable message per
    broken *episode*, sent only once an account is stuck well past the point
    self-healing could still have worked — and its paired recovery message,
    which fires only if that alert actually went out.
    """

    now = datetime.now(timezone.utc)
    prior_failures = prev_status.consecutive_failures if prev_status is not None else 0
    if prior_failures >= _self_heal_after_failures() and _retry_due(
        config.label, now=now
    ):
        invalidate_session(config)
        _LAST_RETRY_ATTEMPT[config.label] = now
        logger.warning(
            "🔑 iCloud account %s broken for %d consecutive refresh(es) (%s) — "
            "forcing a fresh sign-in (backoff %ds)",
            config.label,
            prior_failures,
            prev_status.reason if prev_status else "?",
            _retry_backoff_s(),
        )

    failures = prior_failures + 1  # the streak this poll would extend, if it fails
    # Keep the episode's own start stamp; only a success clears it.
    since = (
        prev_status.failing_since
        if prev_status is not None and prev_status.failing_since
        else now.isoformat()
    )
    try:
        entities = fetch_presence(config=replace(config, request_2fa_push=False))
    except PresenceAuthError as exc:
        logger.warning(
            "⚠️ iCloud account %s needs re-auth (2fa_required): %s", config.label, exc
        )
        entities = []
        status = _account_status(
            config,
            False,
            "2fa_required",
            str(exc),
            consecutive_failures=failures,
            failing_since=since,
        )
    except PresenceConfigError as exc:
        logger.warning(
            "⚠️ iCloud account %s not configured: %s", config.label, exc
        )
        entities = []
        status = _account_status(
            config,
            False,
            "not_configured",
            str(exc),
            consecutive_failures=failures,
            failing_since=since,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "⚠️ Failed to refresh iCloud account %s: %s", config.label, exc
        )
        entities = []
        status = _account_status(
            config,
            False,
            "error",
            str(exc),
            consecutive_failures=failures,
            failing_since=since,
        )
    else:
        status = _account_status(config, True, "ok", "", entity_count=len(entities))

    if status.available:
        # Deliberately not popped from _LAST_RETRY_ATTEMPT (issue #656) — see
        # this function's docstring. The backoff clock keeps running from the
        # last forced handshake regardless of a recovery in between, so a
        # flapping account still gets throttled to one handshake per window.
        if config.label in _ALERTED:
            # Symmetry (#678): the household only hears "restored" for a break
            # it was actually told about. A streak that healed under the alert
            # threshold never surfaced, so its recovery has nothing to close.
            _ALERTED.discard(config.label)
            _notify(
                notifier_factory,
                f"✅ {_account_display_name(config)}'s iCloud Find My connection restored.",
            )
    elif (
        status.consecutive_failures >= _alert_after_failures()
        and config.label not in _ALERTED
    ):
        _ALERTED.add(config.label)
        _notify(notifier_factory, _stuck_alert_text(config, status, now=now))

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
    interval_s = _refresh_interval_s()
    return asyncio.create_task(_run(interval_s), name="presence-refresher")
