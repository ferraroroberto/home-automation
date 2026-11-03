"""The one-shot "Don't arm when away" override store.

Lives in the runtime ``automation`` meta of ``presence_state.json`` beside the
kids-home flag and the arm-key bookkeeping it consumes, because it clears
itself the first time it skips an away-arm.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.presence.presence_engine import (
    _automation_meta,
    _iso,
    _load_state,
    _save_state,
    now_utc,
)


def set_skip_away_arm_override(active: bool, *, at: Optional[datetime] = None) -> None:
    """Persist the one-shot "Don't arm when away" override.

    Runtime ``automation`` meta like the kids-home flag, because it clears
    itself the first time it skips an away-arm (:func:`record_away_arm_skip`).
    """

    raw = _load_state()
    meta = _automation_meta(raw)
    meta["skip_away_arm_override"] = bool(active)
    meta["skip_away_arm_override_at"] = _iso(at or now_utc())
    _save_state(raw)


def load_skip_away_arm_override() -> bool:
    """Return the one-shot "Don't arm when away" flag (defaults off).

    Raises :class:`StoreUnreadableError` like every state read; the consumer
    treats that as off so a broken store never stops an arm.
    """

    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("skip_away_arm_override", False))


def record_away_arm_skip(key: str) -> bool:
    """Consume the away-arm ``key`` as skipped and clear both overrides.

    One save, so the arm key and the reset land together: were the overrides
    cleared without the key, the next tick would see a pending arm for the same
    departure and arm the house anyway. Kids home clears too because a skip
    leaves the panel disarmed, so its usual reset-on-disarm would never come and
    it would silently carry over to the next away-cycle. Returns whether Kids
    home was on.
    """

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
    return kids_home_was_on
