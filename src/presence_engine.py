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
    """One webhook-backed person's latest confirmed state."""

    person_id: str
    state: str
    updated_at: datetime
    source: str = "webhook"


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
        people[str(person_id)] = PersonPresence(
            person_id=str(person_id),
            state=state,
            updated_at=updated_at,
            source=str(raw.get("source") or "webhook"),
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
    people[clean_id] = {
        "state": clean_state,
        "updated_at": _iso(stamp),
        "source": source,
    }
    raw["people"] = people
    _save_state(raw, path)
    return PersonPresence(clean_id, clean_state, stamp, source)


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
) -> Optional[PresenceDecision]:
    """Return the next alarm action, or ``None`` when no action is safe."""

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
        transition_at = max(p.updated_at for p in home)
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
        all_away_since = max(p.updated_at for p in away)
        if (stamp - all_away_since).total_seconds() < config.arm_away_after_s:
            return None
        key = f"arm:{all_away_since.isoformat()}"
        if key != _last_key("arm") and not _manual_after(all_away_since):
            return PresenceDecision(
                kind="arm",
                action=config.arm_action,
                key=key,
                reason="everyone away past grace",
                transition_at=all_away_since,
            )
    return None


def append_trigger_log(event: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Append one audit event to the gitignored JSONL trigger log."""

    target = Path(path) if path is not None else TRIGGER_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
