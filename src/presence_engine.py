"""Webhook-backed presence state and alarm-transition decisions.

iCloud/Find My remains a cached diagnostic read path. Automation decisions come
from explicit home/away webhooks keyed by stable person ids.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
STATE_PATH = _CONFIG_DIR / "presence_state.json"
AUTOMATION_PATH = _CONFIG_DIR / "presence_automation.json"
TRIGGER_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "presence_triggers.jsonl"

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

    enabled: bool = False
    arm_away_after_s: int = 900
    stale_after_s: int = 3600
    disarm_on_arrival: bool = True
    arm_action: str = "arm"
    disarm_action: str = "disarm"


@dataclass(frozen=True)
class PresenceDecision:
    """One action the alarm consumer should attempt."""

    kind: str
    action: str
    key: str
    reason: str
    transition_at: datetime


def now_utc() -> datetime:
    """Current timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("⚠️ Could not read %s (%s); returning empty", path, exc)
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    logger.info("💾 Saved %s", path)


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
    raw = _read_json(Path(path) if path is not None else STATE_PATH)
    return raw if isinstance(raw, dict) else {}


def _save_state(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    _write_json(Path(path) if path is not None else STATE_PATH, data)


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
    return PersonPresence(clean_id, clean_state, stamp, source, state_since=state_since)


def load_automation_config(path: Optional[Path] = None) -> PresenceAutomationConfig:
    """Return persisted presence automation config, defaulting safely off."""

    raw = _read_json(Path(path) if path is not None else AUTOMATION_PATH)
    if not isinstance(raw, dict):
        raw = {}
    return PresenceAutomationConfig(
        enabled=bool(raw.get("enabled", False)),
        arm_away_after_s=max(0, int(raw.get("arm_away_after_s", 900) or 0)),
        stale_after_s=max(60, int(raw.get("stale_after_s", 3600) or 3600)),
        disarm_on_arrival=bool(raw.get("disarm_on_arrival", True)),
        arm_action=str(raw.get("arm_action") or "arm"),
        disarm_action=str(raw.get("disarm_action") or "disarm"),
    )


def save_automation_config(
    config: PresenceAutomationConfig, path: Optional[Path] = None
) -> None:
    """Persist presence automation config."""

    _write_json(Path(path) if path is not None else AUTOMATION_PATH, asdict(config))


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


def mark_decision_applied(decision: PresenceDecision, outcome: str) -> None:
    """Remember an applied decision key to keep actions edge-triggered."""

    raw = _load_state()
    meta = _automation_meta(raw)
    meta[f"last_{decision.kind}_key"] = decision.key
    meta[f"last_{decision.kind}_outcome"] = outcome
    meta[f"last_{decision.kind}_at"] = _iso(now_utc())
    _save_state(raw)


def _last_key(kind: str) -> str:
    meta = _load_state().get("automation", {})
    return str(meta.get(f"last_{kind}_key") or "") if isinstance(meta, dict) else ""


def _manual_after(transition_at: datetime) -> bool:
    meta = _load_state().get("automation", {})
    if not isinstance(meta, dict):
        return False
    manual_at = _parse_dt(meta.get("manual_alarm_action_at"))
    return manual_at is not None and manual_at >= transition_at


def _fresh_people(
    people: Iterable[PersonPresence],
    *,
    config: PresenceAutomationConfig,
    at: datetime,
) -> list[PersonPresence]:
    return [
        p for p in people
        if (at - p.updated_at.astimezone(timezone.utc)).total_seconds() <= config.stale_after_s
    ]


def evaluate_alarm_decision(
    people: Iterable[PersonPresence],
    *,
    security_mode: str,
    config: PresenceAutomationConfig,
    at: Optional[datetime] = None,
    override_perimeter: bool = False,
) -> Optional[PresenceDecision]:
    """Return the next alarm action, or ``None`` when no action is safe.

    ``override_perimeter`` (the "kids home" toggle) arms perimeter instead of
    full on the everyone-away trigger; the disarm path is unaffected.
    """

    stamp = at or now_utc()
    if not config.enabled:
        return None

    current = list(people)
    if not current:
        return None
    fresh = _fresh_people(current, config=config, at=stamp)
    if len(fresh) != len(current):
        return None

    home = [p for p in fresh if p.state == "home"]
    away = [p for p in fresh if p.state == "away"]

    if home and config.disarm_on_arrival and security_mode != "disarmed":
        # Transition time, not last-seen — so a deliberate (scheduled/manual) arm
        # while people are already home isn't undone, and the key stays stable
        # across pings. A genuine away→home arrival advances state_since and lets
        # the disarm fire once, as intended.
        transition_at = max(p.state_since for p in home)
        key = f"disarm:{transition_at.isoformat()}"
        if key != _last_key("disarm") and not _manual_after(transition_at):
            return PresenceDecision(
                kind="disarm",
                action=config.disarm_action,
                key=key,
                reason="first confirmed arrival",
                transition_at=transition_at,
            )
        return None

    if len(away) == len(fresh) and security_mode == "disarmed":
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


def append_trigger_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append one audit event to the gitignored presence-trigger JSONL log.

    Delegates to the shared :mod:`src.activity_log` writer so there is a single
    append-only JSONL implementation across the app; the presence trigger log
    keeps its own filename and fields.
    """

    from src.activity_log import append_activity

    target = Path(path) if path is not None else TRIGGER_LOG_PATH
    append_activity("presence", event, path=target)
