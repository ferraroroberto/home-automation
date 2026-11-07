"""The one-shot "Don't arm when away" override store.

Lives in the runtime ``automation`` meta of ``presence_state.json`` beside the
kids-home flag and the arm-key bookkeeping it consumes. It never outlives one
away/return cycle: it clears the first time it skips an away-arm, or on the
first arrival after it was set, whichever comes first.

A clear whose save fails leaves the store saying "on", and this process keeps a
note of the failure so the API reports the clear as not confirmed instead of
letting a retry loop look like a forgotten switch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from src._schedule_store import StoreUnreadableError
from src.presence.presence_engine import (
    PersonPresence,
    _automation_meta,
    _iso,
    _load_state,
    _parse_dt,
    _save_state,
    now_utc,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkipAwayArmStatus:
    """What the API shows: on/off, when it was set, and an unconfirmed clear."""

    active: bool
    set_at: Optional[datetime]
    clear_failed_at: Optional[datetime]
    clear_error: Optional[str]


@dataclass
class _ClearFailure:
    at: Optional[datetime] = None
    error: Optional[str] = None


# Process-local by design: when the store cannot be written, the store itself
# cannot carry the news that its clear failed.
_CLEAR_FAILURE = _ClearFailure()


def _forget_clear_failure() -> None:
    _CLEAR_FAILURE.at = None
    _CLEAR_FAILURE.error = None


def note_clear_failure(exc: Exception) -> None:
    """Remember that a clear could not be saved, for the API's not-confirmed state."""

    _CLEAR_FAILURE.at = now_utc()
    _CLEAR_FAILURE.error = str(exc)


def set_skip_away_arm_override(active: bool, *, at: Optional[datetime] = None) -> None:
    """Persist the one-shot "Don't arm when away" override.

    ``skip_away_arm_override_at`` is the set time while it is on (the arrival
    clear compares against it) and the clear time once it is off.
    """

    raw = _load_state()
    meta = _automation_meta(raw)
    stamp = at or now_utc()
    meta["skip_away_arm_override"] = bool(active)
    meta["skip_away_arm_override_at"] = _iso(stamp)
    _save_state(raw)
    _forget_clear_failure()
    logger.info(
        "ℹ️ Don't arm when away switched %s at %s",
        "on" if active else "off",
        _iso(stamp),
    )


def load_skip_away_arm_override() -> bool:
    """Return the one-shot "Don't arm when away" flag (defaults off).

    Raises :class:`StoreUnreadableError` like every state read; the consumer
    treats that as off so a broken store never stops an arm.
    """

    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("skip_away_arm_override", False))


def load_skip_away_arm_status() -> SkipAwayArmStatus:
    """The override plus its set time and any unconfirmed clear.

    Raises :class:`StoreUnreadableError` like :func:`load_skip_away_arm_override`.
    """

    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        meta = {}
    active = bool(meta.get("skip_away_arm_override", False))
    return SkipAwayArmStatus(
        active=active,
        set_at=_parse_dt(meta.get("skip_away_arm_override_at")) if active else None,
        clear_failed_at=_CLEAR_FAILURE.at if active else None,
        clear_error=_CLEAR_FAILURE.error if active else None,
    )


def arrival_after(
    people: Iterable[PersonPresence], set_at: Optional[datetime]
) -> Optional[PersonPresence]:
    """The person whose arrival home came after ``set_at``, if any.

    People already home when the override was set do not count: their
    ``state_since`` predates it. No set time means no comparison is possible,
    so nothing counts as an arrival.
    """

    if set_at is None:
        return None
    arrived = [p for p in people if p.state == "home" and p.state_since > set_at]
    return max(arrived, key=lambda p: p.state_since) if arrived else None


def clear_skip_away_arm_on_arrival(
    people: Iterable[PersonPresence],
) -> Optional[PersonPresence]:
    """Clear the override if someone came home after it was set.

    Returns the arriving person when it cleared, ``None`` when there was nothing
    to clear. A failed read or save raises (and is noted for the API) with the
    override left on; the caller retries next tick.
    """

    try:
        raw = _load_state()
        meta = _automation_meta(raw)
        if not bool(meta.get("skip_away_arm_override", False)):
            return None
        person = arrival_after(people, _parse_dt(meta.get("skip_away_arm_override_at")))
        if person is None:
            return None
        meta["skip_away_arm_override"] = False
        meta["skip_away_arm_override_at"] = _iso(now_utc())
        _save_state(raw)
    except (StoreUnreadableError, OSError) as exc:
        note_clear_failure(exc)
        raise
    _forget_clear_failure()
    return person


def record_away_arm_skip(key: str) -> bool:
    """Consume the away-arm ``key`` as skipped and clear both overrides.

    One save, so the arm key and the reset land together: were the overrides
    cleared without the key, the next tick would see a pending arm for the same
    departure and arm the house anyway. Kids home clears too because a skip
    leaves the panel disarmed, so its usual reset-on-disarm would never come and
    it would silently carry over to the next away-cycle. Returns whether Kids
    home was on. A failed read or save raises and is noted for the API.
    """

    try:
        raw = _load_state()
        meta = _automation_meta(raw)
        stamp = _iso(now_utc())
        kids_home_was_on = bool(meta.get("kids_home_override", False))
        meta["last_arm_key"] = key
        meta["last_arm_outcome"] = "skipped"
        meta["last_arm_at"] = stamp
        meta["skip_away_arm_override"] = False
        meta["skip_away_arm_override_at"] = stamp
        if kids_home_was_on:
            meta["kids_home_override"] = False
            meta["kids_home_override_at"] = stamp
        _save_state(raw)
    except (StoreUnreadableError, OSError) as exc:
        note_clear_failure(exc)
        raise
    _forget_clear_failure()
    return kids_home_was_on
