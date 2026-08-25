"""Webhook-backed presence state and alarm-transition decisions.

iCloud/Find My remains a cached diagnostic read path. Automation decisions come
from explicit home/away webhooks keyed by stable person ids. The one exception
(issue #653) is staleness corroboration: when a person's webhook data has gone
stale, the caller may supply a per-person :class:`PresenceCorroboration` signal
(built from the live iCloud diagnostics cache) to vouch for it — this module
itself stays iCloud-free; it only ever reads whatever the caller hands it.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from src._schedule_store import StoreUnreadableError, read_json, save_json
from src.presence_roster import remember_people, roster_path_for

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
STATE_PATH = _CONFIG_DIR / "presence_state.json"
AUTOMATION_PATH = _CONFIG_DIR / "presence_automation.json"
TRIGGER_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "presence_triggers.jsonl"

logger = logging.getLogger(__name__)

VALID_STATES = frozenset({"home", "away"})


@dataclass(frozen=True)
class PersonPresence:
    """One webhook-backed person's latest confirmed state.

    ``updated_at`` is the last-seen heartbeat (advances on every webhook ping, so
    the staleness check stays honest). ``state_since`` is the timestamp of the
    last *state change* — it does NOT move on same-state pings, so the alarm
    transition keys are stable. Defaulting it to ``updated_at`` keeps older
    persisted records (and direct constructions) working unchanged.
    """

    person_id: str
    state: str
    updated_at: datetime
    source: str = "webhook"
    state_since: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.state_since is None:
            object.__setattr__(self, "state_since", self.updated_at)


@dataclass(frozen=True)
class PresenceAutomationConfig:
    """Alarm automation knobs persisted in ``config/presence_automation.json``."""

    auto_arm_enabled: bool = False
    arm_away_after_s: int = 900
    stale_after_s: int = 3600
    auto_disarm_enabled: bool = False
    arm_action: str = "arm"
    disarm_action: str = "disarm"
    # Oldest arrival an auto-disarm may still act on (issue #598). ``<= 0``
    # disables the bound. See `evaluate_alarm_decision` for why this is a
    # disarm-only knob.
    disarm_max_age_s: int = 900
    # How long an auto-arm block must persist before it is worth telling anyone
    # about (issue #599). "One person home, another away" is the *normal* state
    # of a partly-occupied house; it is only diagnostic once it sticks. ``0``
    # notifies immediately (the pre-#599 behaviour).
    arm_block_notify_after_s: int = 900
    # How old a corroborating iCloud/Find My fix may be and still vouch for a
    # stale webhook person (issue #653). The webhook write path is edge-
    # triggered only (Arrive/Leave geofence crossings, no heartbeat), so a
    # person who simply stays put past ``stale_after_s`` isn't broken - this
    # lets a fresher, agreeing iCloud read stand in for the missing heartbeat
    # instead of the engine refusing every decision for the whole household.
    icloud_corroboration_window_s: int = 21600


@dataclass(frozen=True)
class PresenceCorroboration:
    """One person's iCloud/Find My corroboration signal (issue #653).

    Supplied per-tick by the caller (``app/webapp/presence_automation.py``,
    which already reads the iCloud diagnostics cache) — this module never
    fetches iCloud data itself, it only reads whatever signal it's handed.
    """

    last_seen: datetime
    at_home: Optional[bool]


@dataclass(frozen=True)
class PresenceDecision:
    """One action the alarm consumer should attempt."""

    kind: str
    action: str
    key: str
    reason: str
    transition_at: datetime


@dataclass(frozen=True)
class PresenceBlock:
    """Diagnostic: identifies who is keeping an otherwise-eligible auto-arm
    from firing (issue #531). A tracked person's presence can get stuck
    ``home`` indefinitely (their device's "leave" webhook never fires, even
    while its "arrive" heartbeat keeps pinging) - from the engine's own
    perspective that is indistinguishable from them genuinely still being
    home, so it correctly refuses to arm. This surfaces *that* stuck state so
    a human can tell the difference, instead of the block being silent.
    """

    key: str
    blocking_person_ids: tuple[str, ...]
    since: datetime


def now_utc() -> datetime:
    """Current timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    raw = read_json(Path(path) if path is not None else STATE_PATH, {})
    return raw if isinstance(raw, dict) else {}


def _save_state(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    save_json(Path(path) if path is not None else STATE_PATH, data)


def _roster_path(path: Optional[Path] = None) -> Path:
    """The roster beside the state file this module is actually using.

    Resolving it through ``STATE_PATH`` rather than the roster's own default is
    what keeps the two stores together: anything that redirects presence state
    — a test, a worktree's copied config — redirects the roster with it, and
    can never reach the real household's file.
    """

    return roster_path_for(Path(path) if path is not None else STATE_PATH)


def remember_known_people(person_ids: Iterable[str]) -> Tuple[str, ...]:
    """Union ``person_ids`` into the roster, returning the full known set."""

    return remember_people(person_ids, _roster_path())


def load_people(path: Optional[Path] = None) -> Dict[str, PersonPresence]:
    """Return webhook-backed people keyed by person id."""

    raw_people = _load_state(path).get("people", {})
    if not isinstance(raw_people, dict):
        return {}
    people: Dict[str, PersonPresence] = {}
    for person_id, raw in raw_people.items():
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state") or "")
        updated_at = _parse_dt(raw.get("updated_at"))
        if state not in VALID_STATES or updated_at is None:
            continue
        # Older records predate state_since — fall back to updated_at for them.
        state_since = _parse_dt(raw.get("state_since")) or updated_at
        people[str(person_id)] = PersonPresence(
            person_id=str(person_id),
            state=state,
            updated_at=updated_at,
            source=str(raw.get("source") or "webhook"),
            state_since=state_since,
        )
    return people


def set_person_state(
    person_id: str,
    state: str,
    *,
    at: Optional[datetime] = None,
    source: str = "webhook",
    path: Optional[Path] = None,
) -> PersonPresence:
    """Persist one confirmed person state from a webhook."""

    clean_id = person_id.strip()
    clean_state = state.strip().lower()
    if not clean_id:
        raise ValueError("person_id is required")
    if clean_state not in VALID_STATES:
        raise ValueError("state must be 'home' or 'away'")
    stamp = (at or now_utc()).astimezone(timezone.utc)
    raw = _load_state(path)
    people = raw.get("people")
    if not isinstance(people, dict):
        people = {}
    # state_since moves only on a real state change; a same-state ping refreshes
    # the heartbeat (updated_at) but keeps the original transition timestamp, so
    # the alarm transition keys don't churn (else a scheduled arm gets undone by
    # the next presence ping). A brand-new person starts state_since = now.
    prior = people.get(clean_id)
    prior_state = prior.get("state") if isinstance(prior, dict) else None
    if prior_state == clean_state:
        state_since = (
            _parse_dt(prior.get("state_since"))
            or _parse_dt(prior.get("updated_at"))
            or stamp
        )
    else:
        state_since = stamp
    people[clean_id] = {
        "state": clean_state,
        "updated_at": _iso(stamp),
        "state_since": _iso(state_since),
        "source": source,
    }
    raw["people"] = people
    _save_state(raw, path)
    # The roster is what lets the engine notice this person going *missing*
    # later (issue #689), so it has to learn them at the same moment their
    # state does — beside whichever state file this call actually wrote.
    #
    # Guarded because the state write above has already landed: raising here
    # would fail the webhook for a change that succeeded, and HA's rest_command
    # would read that as a lost update. The automation tick unions the roster
    # every round anyway, so the registration is picked up within ~10s.
    try:
        remember_people([clean_id], _roster_path(path))
    except StoreUnreadableError as exc:
        logger.warning("⚠️ Could not register %s in the presence roster: %s", clean_id, exc)
    return PersonPresence(clean_id, clean_state, stamp, source, state_since=state_since)


def _int_or(value: Any, fallback: int) -> int:
    """Coerce untrusted JSON to an int, falling back on null/garbage."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_automation_config(path: Optional[Path] = None) -> PresenceAutomationConfig:
    """Return persisted presence automation config, defaulting safely off."""

    raw = read_json(Path(path) if path is not None else AUTOMATION_PATH, {})
    if not isinstance(raw, dict):
        raw = {}
    if "auto_arm_enabled" in raw or "auto_disarm_enabled" in raw:
        auto_arm_enabled = bool(raw.get("auto_arm_enabled", False))
        auto_disarm_enabled = bool(raw.get("auto_disarm_enabled", False))
    else:
        # Legacy shape: one master `enabled` flag gated both directions, with
        # `disarm_on_arrival` as a sub-flag only meaningful while it was on.
        # Migrate on read so an old persisted file keeps behaving the same
        # way it always did; the file itself is rewritten to the new shape
        # the next time the user saves a change.
        legacy_enabled = bool(raw.get("enabled", False))
        auto_arm_enabled = legacy_enabled
        auto_disarm_enabled = legacy_enabled and bool(raw.get("disarm_on_arrival", True))
    return PresenceAutomationConfig(
        auto_arm_enabled=auto_arm_enabled,
        arm_away_after_s=max(0, int(raw.get("arm_away_after_s", 900) or 0)),
        stale_after_s=max(60, int(raw.get("stale_after_s", 3600) or 3600)),
        auto_disarm_enabled=auto_disarm_enabled,
        arm_action=str(raw.get("arm_action") or "arm"),
        disarm_action=str(raw.get("disarm_action") or "disarm"),
        # An absent *or malformed* key must fall back to the bounded default,
        # never to "unbounded" — a config written before #598, or one with a
        # null/garbage value, is exactly the case the bound exists to protect.
        # (The neighbouring `int(... or 0)` idiom would turn a null into 0,
        # which here means *disabled* — the wrong direction for a safety bound.)
        disarm_max_age_s=_int_or(raw.get("disarm_max_age_s"), 900),
        arm_block_notify_after_s=max(0, _int_or(raw.get("arm_block_notify_after_s"), 900)),
        icloud_corroboration_window_s=max(
            0, _int_or(raw.get("icloud_corroboration_window_s"), 21600)
        ),
    )


def save_automation_config(
    config: PresenceAutomationConfig, path: Optional[Path] = None
) -> None:
    """Persist presence automation config."""

    save_json(Path(path) if path is not None else AUTOMATION_PATH, asdict(config))


def _automation_meta(raw: Dict[str, Any]) -> Dict[str, Any]:
    meta = raw.get("automation")
    if not isinstance(meta, dict):
        meta = {}
        raw["automation"] = meta
    return meta


def note_manual_alarm_action(action: str, *, at: Optional[datetime] = None) -> None:
    """Record a manual alarm command so automation does not immediately undo it."""

    raw = _load_state()
    meta = _automation_meta(raw)
    meta["manual_alarm_action"] = action
    meta["manual_alarm_action_at"] = _iso(at or now_utc())
    _save_state(raw)


def set_kids_home_override(active: bool, *, at: Optional[datetime] = None) -> None:
    """Persist the transient 'kids home' override (arm perimeter, not full).

    Lives in the runtime ``automation`` meta — not the persisted config knobs —
    because it is auto-reset on the next disarm-on-arrival.
    """

    raw = _load_state()
    meta = _automation_meta(raw)
    meta["kids_home_override"] = bool(active)
    meta["kids_home_override_at"] = _iso(at or now_utc())
    _save_state(raw)


def load_kids_home_override() -> bool:
    """Return the transient 'kids home' override flag (defaults off)."""

    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("kids_home_override", False))


def _mark_key(kind: str, key: str, outcome: str) -> None:
    """The single writer for the ``last_<kind>_*`` edge-trigger bookkeeping."""

    raw = _load_state()
    meta = _automation_meta(raw)
    meta[f"last_{kind}_key"] = key
    meta[f"last_{kind}_outcome"] = outcome
    meta[f"last_{kind}_at"] = _iso(now_utc())
    _save_state(raw)


def mark_decision_applied(decision: PresenceDecision, outcome: str) -> None:
    """Remember an applied decision key to keep actions edge-triggered."""

    _mark_key(decision.kind, decision.key, outcome)


def mark_disarm_satisfied(key: str) -> None:
    """Consume a disarm key that an already-disarmed panel made moot (#598).

    Recording it here is what stops it from sitting pending until the panel is
    next armed — see :func:`satisfied_disarm_key`.
    """

    _mark_key("disarm", key, "already disarmed")


def _last_key(kind: str) -> str:
    meta = _load_state().get("automation", {})
    return str(meta.get(f"last_{kind}_key") or "") if isinstance(meta, dict) else ""


def _manual_after(transition_at: datetime) -> bool:
    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        return False
    manual_at = _parse_dt(meta.get("manual_alarm_action_at"))
    return manual_at is not None and manual_at >= transition_at


def _older_than(transition_at: datetime, stamp: datetime, max_age_s: int) -> bool:
    """True when ``transition_at`` predates ``stamp`` by more than ``max_age_s``.

    ``max_age_s <= 0`` means unbounded — the explicit opt-out.
    """

    if max_age_s <= 0:
        return False
    return (stamp - transition_at).total_seconds() > max_age_s


def _corroborated(
    person: PersonPresence,
    *,
    config: PresenceAutomationConfig,
    at: datetime,
    corroboration: Dict[str, PresenceCorroboration],
) -> bool:
    """True when a stale person's last known state is vouched for by a fresh,
    agreeing iCloud/Find My signal (issue #653)."""

    signal = corroboration.get(person.person_id)
    if signal is None:
        return False
    age_s = (at - signal.last_seen.astimezone(timezone.utc)).total_seconds()
    if age_s > config.icloud_corroboration_window_s:
        return False
    expected_at_home = person.state == "home"
    return signal.at_home is not None and bool(signal.at_home) == expected_at_home


def missing_people(
    people: Iterable[PersonPresence], known_person_ids: Iterable[str]
) -> tuple[str, ...]:
    """Roster members with no record at all in the current presence state (#689).

    The freshness gate below can only reason about people it can *see*: a
    person whose record has vanished outright looks exactly like a household
    that never had them. Comparing against the roster is what turns that
    silence back into a fact — and every caller below treats a non-empty
    result the way it already treats a stale person, by refusing to act.
    """

    present = {p.person_id for p in people}
    known = {str(pid).strip() for pid in known_person_ids if str(pid).strip()}
    return tuple(sorted(known - present))


def _fresh_people(
    people: Iterable[PersonPresence],
    *,
    config: PresenceAutomationConfig,
    at: datetime,
    corroboration: Optional[Dict[str, PresenceCorroboration]] = None,
) -> list[PersonPresence]:
    corroboration = corroboration or {}
    fresh: list[PersonPresence] = []
    for p in people:
        age_s = (at - p.updated_at.astimezone(timezone.utc)).total_seconds()
        if age_s <= config.stale_after_s or _corroborated(
            p, config=config, at=at, corroboration=corroboration
        ):
            fresh.append(p)
    return fresh


def evaluate_alarm_decision(
    people: Iterable[PersonPresence],
    *,
    security_mode: str,
    config: PresenceAutomationConfig,
    at: Optional[datetime] = None,
    override_perimeter: bool = False,
    corroboration: Optional[Dict[str, PresenceCorroboration]] = None,
    known_person_ids: Iterable[str] = (),
) -> Optional[PresenceDecision]:
    """Return the next alarm action, or ``None`` when no action is safe.

    ``override_perimeter`` (the "kids home" toggle) arms perimeter instead of
    full on the everyone-away trigger; the disarm path is unaffected.
    ``corroboration`` (issue #653) lets a stale person's last known state
    stand in as fresh when a fresher, agreeing iCloud/Find My read vouches for
    it — see :func:`_corroborated`.
    ``known_person_ids`` (issue #689) is the roster this household is supposed
    to be tracking; a member with no record at all refuses every decision, the
    same way a stale one does.
    """

    stamp = at or now_utc()
    if not config.auto_arm_enabled and not config.auto_disarm_enabled:
        return None

    current = list(people)
    if not current:
        return None
    # A shrunken roster is not a smaller household (issue #689). Refuse both
    # directions, not just arm: refusing a disarm leaves the house armed, which
    # is the safe way to be wrong about who is in it.
    if missing_people(current, known_person_ids):
        return None
    fresh = _fresh_people(current, config=config, at=stamp, corroboration=corroboration)
    if len(fresh) != len(current):
        return None

    home = [p for p in fresh if p.state == "home"]
    away = [p for p in fresh if p.state == "away"]

    if home and config.auto_disarm_enabled and security_mode != "disarmed":
        # Transition time, not last-seen — so a deliberate (scheduled/manual) arm
        # while people are already home isn't undone, and the key stays stable
        # across pings. A genuine away→home arrival advances state_since and lets
        # the disarm fire once, as intended.
        transition_at = max(p.state_since for p in home)
        key = f"disarm:{transition_at.isoformat()}"
        if key == _last_key("disarm") or _manual_after(transition_at):
            return None
        # Freshness bound (#598). An arrival the engine never got to act on -
        # because the panel was already disarmed when it landed - stays pending
        # indefinitely and fires the moment the panel is next armed. That is how
        # a 22:43 keypad arm was undone by a 19:51 arrival: nearly three hours
        # later, reported as a "first confirmed arrival". An arrival that old is
        # not news, so refuse it outright.
        #
        # Deliberately NOT applied to the arm path below, and it must stay that
        # way: refusing a stale disarm leaves the house armed (fails safe), but
        # refusing a stale arm would leave an empty house unarmed after any
        # webapp downtime longer than the bound (fails unsafe). The two
        # directions are asymmetric on purpose - do not "symmetrise" this.
        if _older_than(transition_at, stamp, config.disarm_max_age_s):
            return None
        return PresenceDecision(
            kind="disarm",
            action=config.disarm_action,
            key=key,
            reason="first confirmed arrival",
            transition_at=transition_at,
        )

    if len(away) == len(fresh) and config.auto_arm_enabled and security_mode == "disarmed":
        # When everyone left (transition time), not last-seen — so the arm-away
        # grace counts from the actual departure and same-state pings can't keep
        # resetting it.
        all_away_since = max(p.state_since for p in away)
        if (stamp - all_away_since).total_seconds() < config.arm_away_after_s:
            return None
        key = f"arm:{all_away_since.isoformat()}"
        if key != _last_key("arm") and not _manual_after(all_away_since):
            return PresenceDecision(
                kind="arm",
                action="perimeter" if override_perimeter else config.arm_action,
                key=key,
                reason=(
                    "everyone away past grace (kids-home override)"
                    if override_perimeter
                    else "everyone away past grace"
                ),
                transition_at=all_away_since,
            )
    return None


def satisfied_disarm_key(
    people: Iterable[PersonPresence],
    *,
    security_mode: str,
    config: PresenceAutomationConfig,
    at: Optional[datetime] = None,
    corroboration: Optional[Dict[str, PresenceCorroboration]] = None,
    known_person_ids: Iterable[str] = (),
) -> Optional[str]:
    """The disarm key an already-disarmed panel has made moot (issue #598).

    :func:`evaluate_alarm_decision` only records a disarm key when it *acts* on
    it. An arrival that lands while the panel is already disarmed therefore
    produces a key that is never recorded — there is nothing to do — and it
    stays pending until the panel is next armed, at which point it fires and
    undoes that arm. The observed case: two people arriving 32 s apart, the
    second arrival left pending for nearly three hours.

    Returning the key here lets the consumer record it as satisfied at the
    moment it becomes moot, which is the honest bookkeeping: the condition
    "someone is home and the panel should be disarmed" *is* met, just not by us.

    Applies the decision path's *input* gates (enabled, freshness) so it can
    only ever consume a key that path would itself have considered. It
    deliberately does not check ``_manual_after``: that timestamp only moves
    forward, so consuming can never do anything but prevent a deliberate arm
    from being undone. ``None`` when there is nothing to consume.
    """

    if not config.auto_disarm_enabled or security_mode != "disarmed":
        return None
    stamp = at or now_utc()
    current = list(people)
    if not current:
        return None
    if missing_people(current, known_person_ids):
        return None
    fresh = _fresh_people(current, config=config, at=stamp, corroboration=corroboration)
    if len(fresh) != len(current):
        return None
    home = [p for p in fresh if p.state == "home"]
    if not home:
        return None
    key = f"disarm:{max(p.state_since for p in home).isoformat()}"
    return None if key == _last_key("disarm") else key


def evaluate_arm_block(
    people: Iterable[PersonPresence],
    *,
    security_mode: str,
    config: PresenceAutomationConfig,
    at: Optional[datetime] = None,
    corroboration: Optional[Dict[str, PresenceCorroboration]] = None,
    known_person_ids: Iterable[str] = (),
) -> Optional[PresenceBlock]:
    """Diagnose why an otherwise-armable house hasn't auto-armed (issue #531).

    Fires only on the specific shape "someone left, but the panel is still
    disarmed because at least one other fresh tracked person is still home" -
    the everyone-away condition ``evaluate_alarm_decision`` requires can't be
    satisfied yet, but it isn't obviously *wrong* either from a house that's
    only partly empty for a normal reason. It does not fire when everyone is
    home (nothing has left, nothing to block) or everyone is away (arm would
    already fire) or presence data is stale and uncorroborated (a distinct
    case surfaced by :func:`evaluate_staleness_block` instead, not this one).
    """

    if not config.auto_arm_enabled or security_mode != "disarmed":
        return None
    stamp = at or now_utc()
    current = list(people)
    if not current:
        return None
    # A missing roster member is :func:`evaluate_staleness_block`'s story to
    # tell, not this one's — reporting "ana is still home" while the engine has
    # lost sight of roberto entirely would name the wrong blocker.
    if missing_people(current, known_person_ids):
        return None
    fresh = _fresh_people(current, config=config, at=stamp, corroboration=corroboration)
    if len(fresh) != len(current):
        return None
    home = [p for p in fresh if p.state == "home"]
    away = [p for p in fresh if p.state == "away"]
    if not home or not away:
        return None
    since = min(p.state_since for p in home)
    blocking_ids = tuple(sorted(p.person_id for p in home))
    key = f"block:{','.join(blocking_ids)}:{since.isoformat()}"
    return PresenceBlock(key=key, blocking_person_ids=blocking_ids, since=since)


@dataclass(frozen=True)
class StalePresenceBlock:
    """Diagnostic: which tracked people the engine can't currently account for.

    Two distinct ways to lose sight of someone, reported together because they
    block automation identically and a household only cares that *someone* has
    gone dark:

    ``stale_person_ids`` — their webhook data aged out and no iCloud read
    corroborates it (issue #653). The engine knows where they last were and
    can't vouch for it.

    ``missing_person_ids`` — they have no record at all, though the roster says
    they should (issue #689). The engine doesn't know they exist any more. This
    is the strictly worse one: a stale person still blocks the freshness gate,
    while a vanished person used to be invisible to every gate, which is how
    the house armed itself on a household of two with one person asleep inside.

    The companion to :class:`PresenceBlock`, which diagnoses the benign "someone
    fresh is still home".
    """

    key: str
    stale_person_ids: tuple[str, ...]
    missing_person_ids: tuple[str, ...] = ()

    @property
    def all_person_ids(self) -> tuple[str, ...]:
        """Everyone this block is about, however they went dark."""

        return tuple(sorted(set(self.stale_person_ids) | set(self.missing_person_ids)))


def evaluate_staleness_block(
    people: Iterable[PersonPresence],
    *,
    config: PresenceAutomationConfig,
    at: Optional[datetime] = None,
    corroboration: Optional[Dict[str, PresenceCorroboration]] = None,
    known_person_ids: Iterable[str] = (),
) -> Optional[StalePresenceBlock]:
    """Diagnose whether presence data the engine can't establish is what's
    blocking automation (issues #653, #689). Fires whenever a tracked person
    fails the freshness gate ``evaluate_alarm_decision``/``evaluate_arm_block``
    apply, **or** a roster member has no record at all — i.e. exactly the
    conditions that otherwise stop the household's automation, or silently
    narrow it, with no trace anywhere.
    """

    if not config.auto_arm_enabled and not config.auto_disarm_enabled:
        return None
    stamp = at or now_utc()
    current = list(people)
    # Deliberately no "nothing to say when there are no people" early return:
    # an empty state file with a non-empty roster is the *loudest* case there
    # is — the whole household's records are gone — and it must reach Telegram
    # rather than be filed as "nobody is configured".
    missing_ids = missing_people(current, known_person_ids)
    fresh = _fresh_people(current, config=config, at=stamp, corroboration=corroboration)
    fresh_ids = {p.person_id for p in fresh}
    stale_ids = tuple(sorted(p.person_id for p in current if p.person_id not in fresh_ids))
    if not stale_ids and not missing_ids:
        return None
    key = f"stale:{','.join(stale_ids)}|missing:{','.join(missing_ids)}"
    return StalePresenceBlock(
        key=key, stale_person_ids=stale_ids, missing_person_ids=missing_ids
    )


# Floor between retried block-notification attempts once one is due (issue
# #601) — independent of ``dwell_s``, which only gates the *first* attempt.
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
        # Both diagnostics run every ~10s tick and, before issue #689, saved
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


def load_staleness_block() -> Dict[str, Any]:
    """Return the persisted stale-presence-block diagnostic, or all-clear."""

    return _STALE_BLOCK.load()


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


def append_trigger_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append one audit event to the gitignored presence-trigger JSONL log.

    Delegates to the shared :mod:`src.activity_log` writer so there is a single
    append-only JSONL implementation across the app; the presence trigger log
    keeps its own filename and fields.
    """

    from src.activity_log import append_activity

    target = Path(path) if path is not None else TRIGGER_LOG_PATH
    append_activity("presence", event, path=target)
