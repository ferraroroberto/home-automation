"""Persisted notify state for the presence block diagnostics.

:mod:`src.presence.presence_engine` decides *whether* automation is blocked
(:func:`~src.presence.presence_engine.evaluate_arm_block`,
:func:`~src.presence.presence_engine.evaluate_staleness_block`); this module
remembers each block episode in the presence state file and decides when a
notification about it is due.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from src.presence.presence_engine import (
    PresenceBlock,
    StalePresenceBlock,
    _automation_meta,
    _iso,
    _load_state,
    _parse_dt,
    _save_state,
    now_utc,
)


# Floor between retried block-notification attempts once one is due,
# independent of ``dwell_s``, which only gates the *first* attempt.
# Without this, a send that keeps declining or failing (Telegram down, no
# notifier configured, the ``error`` toggle off) would be re-attempted on
# every ~10s presence poll tick instead of backing off. Shared by both block
# namespaces below.
_ARM_BLOCK_RETRY_COOLDOWN_S = 300


@dataclass(frozen=True)
class ArmBlockObservation:
    """What one :func:`set_arm_block` / :func:`set_staleness_block` call observed.

    ``changed`` is a *new episode* (newly appeared, different blocking people,
    or newly cleared) — the log-once signal. ``notify`` additionally requires
    the episode to have persisted for the configured dwell, not to have been
    confirmed notified already, and not to be cooling down from a recent
    attempt — which is what separates "someone is arriving" from "someone's
    presence is stuck" (issue #599). ``notify=True`` means a notification is
    *due*, not that one was sent — the caller must attempt the send itself and
    report the outcome via :func:`mark_arm_block_notified` (issue #601).
    """

    changed: bool
    notify: bool


@dataclass(frozen=True)
class _BlockNamespace:
    """One block-diagnostic notification state machine over its own key space.

    The arm-block ("someone fresh is still home") and stale-block ("the engine
    can't tell what's going on with someone") diagnostics run the *same*
    changed / notify / dwell / cooldown contract and must never share a notify
    latch — so each persists under an independent ``<prefix>_blocked_*`` key
    namespace. That independence is what the ``prefix`` argument buys (issue
    #664); it used to be bought by a verbatim copy of the whole machine, which
    is exactly the kind of clone that drifts the moment one side is fixed.

    ``person_ids_attr`` is the block dataclass's ids field
    (:class:`PresenceBlock` names it ``blocking_person_ids``,
    :class:`StalePresenceBlock` ``stale_person_ids``). ``track_since`` adds the
    ``<prefix>_blocked_since`` key: the arm-block diagnostic surfaces the
    blocking person's ``state_since`` in the API payload, while the stale one
    has no equivalent timestamp and must not grow a phantom key.
    """

    prefix: str
    person_ids_attr: str
    track_since: bool = False

    @property
    def _flag(self) -> str:
        return f"{self.prefix}_blocked"

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}_blocked_{suffix}"

    def load(self) -> Dict[str, Any]:
        """Return the persisted diagnostic, or the all-clear default."""

        meta = _load_state().get("automation", {})
        if not isinstance(meta, dict):
            meta = {}
        payload: Dict[str, Any] = {
            "blocked": bool(meta.get(self._flag, False)),
            "person_ids": list(meta.get(self._key("person_ids")) or []),
        }
        if self.track_since:
            payload["since"] = meta.get(self._key("since"))
        return payload

    def mark_notified(self, key: str) -> None:
        raw = _load_state()
        meta = _automation_meta(raw)
        if str(meta.get(self._key("key")) or "") != key:
            return  # the episode has already moved on; nothing to mark
        meta[self._key("notified")] = True
        _save_state(raw)

    def mark_attempted(self, key: str, *, at: Optional[datetime] = None) -> None:
        raw = _load_state()
        meta = _automation_meta(raw)
        if str(meta.get(self._key("key")) or "") != key:
            return
        meta[self._key("last_attempt_at")] = _iso(at or now_utc())
        _save_state(raw)

    def set(
        self,
        block: Optional[Any],
        *,
        dwell_s: int = 0,
        at: Optional[datetime] = None,
    ) -> ArmBlockObservation:
        stamp = at or now_utc()
        raw = _load_state()
        meta = _automation_meta(raw)
        # Both diagnostics run every ~10s tick and, previously, saved
        # unconditionally — rewriting the whole presence state four times a tick
        # to persist bytes identical to the ones just read. That churn is what
        # kept `presence_state.json` permanently mid-`os.replace` and made a
        # reader's sharing violation near-certain. Compare and skip instead.
        before = deepcopy(meta)
        prior_key = str(meta.get(self._key("key")) or "")
        if block is None:
            changed = prior_key != ""
            meta[self._flag] = False
            meta[self._key("person_ids")] = []
            if self.track_since:
                meta[self._key("since")] = None
            meta[self._key("key")] = ""
            meta[self._key("first_seen")] = None
            meta[self._key("notified")] = False
            meta[self._key("last_attempt_at")] = None
            if meta != before:
                _save_state(raw)
            return ArmBlockObservation(changed=changed, notify=False)

        changed = prior_key != block.key
        if changed:
            first_seen, notified, last_attempt = stamp, False, None
        else:
            first_seen = _parse_dt(meta.get(self._key("first_seen"))) or stamp
            notified = bool(meta.get(self._key("notified")))
            last_attempt = _parse_dt(meta.get(self._key("last_attempt_at")))
        due = (stamp - first_seen).total_seconds() >= dwell_s
        cooling_down = (
            last_attempt is not None
            and (stamp - last_attempt).total_seconds() < _ARM_BLOCK_RETRY_COOLDOWN_S
        )
        notify = not notified and due and not cooling_down

        meta[self._flag] = True
        meta[self._key("person_ids")] = list(getattr(block, self.person_ids_attr))
        if self.track_since:
            meta[self._key("since")] = _iso(block.since)
        meta[self._key("key")] = block.key
        meta[self._key("first_seen")] = _iso(first_seen)
        if changed:
            meta[self._key("notified")] = False
            meta[self._key("last_attempt_at")] = None
        if meta != before:
            _save_state(raw)
        return ArmBlockObservation(changed=changed, notify=notify)


_ARM_BLOCK = _BlockNamespace(
    prefix="arm", person_ids_attr="blocking_person_ids", track_since=True
)
_STALE_BLOCK = _BlockNamespace(prefix="stale", person_ids_attr="all_person_ids")


def load_arm_block() -> Dict[str, Any]:
    """Return the persisted arm-block diagnostic, or the all-clear default."""

    return _ARM_BLOCK.load()


def mark_arm_block_notified(key: str) -> None:
    """Record that the due arm-block notification for ``key`` was delivered (#601).

    Call only once the caller has confirmed the send actually went through.
    Marking it eagerly — the pre-#601 behaviour, the same shape #527 fixed for
    the per-day error de-dupe — meant a failed or unconfigured notifier
    silently burned the episode's one alert for good, since ``block.key``
    stays stable for as long as the presence itself stays stuck.
    """

    _ARM_BLOCK.mark_notified(key)


def mark_arm_block_attempted(key: str, *, at: Optional[datetime] = None) -> None:
    """Stamp the last arm-block notification attempt for ``key`` (#601).

    Call on every attempt regardless of outcome — this is what backs the
    ``_ARM_BLOCK_RETRY_COOLDOWN_S`` floor in :func:`set_arm_block`, so a
    declining or persistently-failing send is retried at most once per
    cooldown window instead of on every poll tick.
    """

    _ARM_BLOCK.mark_attempted(key, at=at)


def set_arm_block(
    block: Optional[PresenceBlock],
    *,
    dwell_s: int = 0,
    at: Optional[datetime] = None,
) -> ArmBlockObservation:
    """Persist the current arm-block diagnostic and report what to do about it.

    The dwell clock is anchored to when *this episode* was first observed, not
    to ``block.since``. ``since`` is the blocking person's ``state_since``,
    which can be hours old for someone legitimately at home all day — anchoring
    to it would fire instantly the moment anyone else left, which is the very
    false alert this dwell exists to stop. A changed key restarts the clock.

    ``dwell_s=0`` notifies on first observation — the pre-#599 behaviour.

    Does **not** mark the episode notified itself (#601) — ``notify=True``
    only reports that a send is due; the caller confirms delivery via
    :func:`mark_arm_block_notified`, and should stamp every attempt (sent or
    not) via :func:`mark_arm_block_attempted` so a failing send backs off
    instead of retrying every tick.
    """

    return _ARM_BLOCK.set(block, dwell_s=dwell_s, at=at)


def mark_staleness_block_notified(key: str) -> None:
    """Record that the due stale-block notification for ``key`` was delivered.

    Same confirmed-delivery contract as :func:`mark_arm_block_notified`
    (issue #601's fix, applied here from the start) — kept in an independent
    ``stale_blocked_*`` state namespace so it can never interact with the
    "someone fresh is home" diagnostic's own notify latch.
    """

    _STALE_BLOCK.mark_notified(key)


def mark_staleness_block_attempted(key: str, *, at: Optional[datetime] = None) -> None:
    """Stamp the last stale-block notification attempt for ``key``.

    Backs the same ``_ARM_BLOCK_RETRY_COOLDOWN_S`` floor as the arm-block
    path, so a declining/failing send retries at most once per cooldown
    window instead of on every ~10s poll tick.
    """

    _STALE_BLOCK.mark_attempted(key, at=at)


def set_staleness_block(
    block: Optional[StalePresenceBlock],
    *,
    dwell_s: int = 0,
    at: Optional[datetime] = None,
) -> ArmBlockObservation:
    """Persist the current stale-block diagnostic; the companion to
    :func:`set_arm_block` for the "webhook stale, no iCloud corroboration"
    case (issue #653) — same changed/notify/dwell/cooldown contract, kept in
    its own independent ``stale_blocked_*`` state namespace.
    """

    return _STALE_BLOCK.set(block, dwell_s=dwell_s, at=at)
