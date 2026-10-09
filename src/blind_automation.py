"""Blind group control (#181), daily blind schedules (#871) and the alarm pairing (#875).

UI-free core for moving several Tuya blinds at once — the house-wide
all-up / all-stop / all-down the Blinds card offers — and for the persisted
up/down schedule that drives the same move, following the
``src.hvac_automation`` split: decisions, fan-out and persistence live here;
the thin HTTP surfaces are ``app.webapp.routers.tuya`` and
``app.webapp.routers.blind_schedules``, and the engine that fires due entries
is ``app.webapp.blind_schedules``.

Every blind is a Maxcio-class Tuya curtain switch with exactly one DPS, an
``open/stop/close`` enum and no position feedback, so a group move is just the
per-device :func:`src.tuya_client.set_cover` sent to each blind. The commands
go out **in parallel** (each blocking TinyTuya call in its own worker thread)
rather than one after another, so the blinds start together instead of
rippling across the house one LAN round trip at a time.

One blind failing never stops the others: the result reports each device's
outcome so the caller can say exactly which blind did not move.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from src._schedule_store import clean_days, clean_time, read_json, safe_id, save_json
from src._toggle_prefs import load_toggle_prefs, save_toggle_prefs
from src.tuya_client import (
    TuyaCommandError,
    TuyaConfigError,
    TuyaDeviceNotFoundError,
    list_devices,
    set_cover,
)

logger = logging.getLogger(__name__)

CoverAction = Literal["open", "close", "stop"]
COVER_ACTIONS: tuple[str, ...] = ("open", "close", "stop")


@dataclass(frozen=True)
class BlindOutcome:
    """One blind's result within a group move."""

    device_id: str
    ok: bool
    error: Optional[str] = None


def cover_device_ids() -> List[str]:
    """Every blind in ``devices.json``, deduplicated, in file order.

    A blind is any device with a cover-control DPS mapping — the same test
    ``GET /api/tuya`` uses for its ``has_cover`` flag.
    """
    seen: List[str] = []
    for info in list_devices():
        if info.cover_control_dps is not None and info.device_id and info.device_id not in seen:
            seen.append(info.device_id)
    return seen


async def move_blinds(
    action: CoverAction, device_ids: Optional[Iterable[str]] = None
) -> List[BlindOutcome]:
    """Send ``action`` to every blind in ``device_ids`` (default: all blinds).

    Raises :class:`ValueError` for an unknown action or a device id that is
    not a blind — a caller bug, distinct from a blind that is offline, which
    comes back as an ``ok=False`` outcome instead.
    """
    if action not in COVER_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(COVER_ACTIONS)}")
    known = cover_device_ids()
    targets = known if device_ids is None else list(dict.fromkeys(device_ids))
    unknown = [device_id for device_id in targets if device_id not in known]
    if unknown:
        raise ValueError(f"not a blind: {', '.join(unknown)}")

    async def _one(device_id: str) -> BlindOutcome:
        try:
            await asyncio.to_thread(set_cover, device_id, action)
        except (TuyaCommandError, TuyaConfigError, TuyaDeviceNotFoundError) as exc:
            return BlindOutcome(device_id=device_id, ok=False, error=str(exc))
        return BlindOutcome(device_id=device_id, ok=True)

    outcomes = list(await asyncio.gather(*(_one(device_id) for device_id in targets)))
    failed = [outcome.device_id for outcome in outcomes if not outcome.ok]
    if failed:
        logger.warning(
            "⚠️ Blind group %s: %d of %d blind(s) failed (%s)",
            action, len(failed), len(outcomes), ", ".join(failed),
        )
    else:
        logger.info("✅ Blind group %s sent to %d blind(s)", action, len(outcomes))
    return outcomes


# --------------------------------------------------------------- schedules
SCHEDULES_PATH = Path(__file__).resolve().parent.parent / "config" / "blind_schedules.json"

SCHEDULE_ACTIONS: tuple[str, ...] = ("open", "close")
# When an entry may fire, judged from webhook presence at its fire time:
# always, only with at least one tracked person home, or only with nobody home.
PRESENCE_CONDITIONS: tuple[str, ...] = ("any", "home", "away")


@dataclass(frozen=True)
class BlindScheduleEntry:
    """One daily blind schedule entry: move ``targets`` at ``time`` on ``days``.

    The AC ``ScheduleEntry`` shape (id / enabled / daily HH:MM) plus weekdays,
    an up/down action, the blinds it moves (empty = every blind) and a
    presence condition.
    """

    id: str
    enabled: bool = True
    time: str = "08:00"
    days: Optional[List[str]] = None
    action: str = "open"
    targets: Optional[List[str]] = None
    presence: str = "any"

    def __post_init__(self) -> None:
        object.__setattr__(self, "days", list(self.days) if self.days else clean_days(None))
        object.__setattr__(self, "targets", list(self.targets or []))


def clean_schedule_entry(raw: Dict[str, Any], fallback_id: str) -> BlindScheduleEntry:
    """Coerce untrusted JSON/API data into a :class:`BlindScheduleEntry`."""
    action = str(raw.get("action") or "").strip().lower()
    presence = str(raw.get("presence") or "").strip().lower()
    targets_raw = raw.get("targets")
    targets = (
        list(dict.fromkeys(str(t).strip() for t in targets_raw if str(t).strip()))
        if isinstance(targets_raw, list)
        else []
    )
    return BlindScheduleEntry(
        id=safe_id(raw.get("id"), fallback_id),
        enabled=raw.get("enabled") is not False,
        time=clean_time(raw.get("time"), "08:00"),
        days=clean_days(raw.get("days")),
        action=action if action in SCHEDULE_ACTIONS else "open",
        targets=targets,
        presence=presence if presence in PRESENCE_CONDITIONS else "any",
    )


def load_blind_schedules(path: Optional[Path] = None) -> List[BlindScheduleEntry]:
    """Return the persisted blind schedule list, or ``[]`` if absent."""
    target = Path(path) if path is not None else SCHEDULES_PATH
    raw = read_json(target, [])
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_schedule_entry(item, f"schedule-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def set_blind_schedules(
    raw_entries: List[Dict[str, Any]], path: Optional[Path] = None
) -> List[BlindScheduleEntry]:
    """Replace the whole schedule list with normalized entries and return it."""
    entries = [
        clean_schedule_entry(item, f"schedule-{idx}")
        for idx, item in enumerate(raw_entries, start=1)
        if isinstance(item, dict)
    ]
    target = Path(path) if path is not None else SCHEDULES_PATH
    save_json(target, [asdict(entry) for entry in entries])
    return entries


def presence_allows(condition: str, people_states: Iterable[str]) -> Tuple[Optional[bool], str]:
    """Whether a presence ``condition`` holds for the household's states.

    Returns ``(True, why)`` / ``(False, why)``, or ``(None, why)`` when the
    answer is unknown — no tracked person at all — which a caller must treat
    as its own state, never as "nobody home".
    """
    if condition == "any":
        return True, "no presence condition"
    states = list(people_states)
    if not states:
        return None, "no tracked people, presence unknown"
    someone_home = any(state == "home" for state in states)
    if condition == "home":
        return someone_home, "someone is home" if someone_home else "nobody is home"
    return (not someone_home), "nobody is home" if not someone_home else "someone is home"


# ----------------------------------------------------------- alarm pairing
ALARM_PREFS_PATH = Path(__file__).resolve().parent.parent / "config" / "blind_alarm.json"

# The panel action that is a FULL arm (``src.risco_client.ACTIONS``); a
# perimeter or partial arm leaves someone inside, so the blinds stay up.
_FULL_ARM_ACTION = "arm"


@dataclass(frozen=True)
class BlindAlarmPrefs:
    """The one visible switch for pairing the blinds with the automatic alarm."""

    follow_alarm: bool = False


def load_blind_alarm_prefs(path: Optional[Path] = None) -> BlindAlarmPrefs:
    """Saved pairing prefs, or the defaults (off) when absent."""
    return load_toggle_prefs(BlindAlarmPrefs, Path(path) if path is not None else ALARM_PREFS_PATH)


def save_blind_alarm_prefs(prefs: BlindAlarmPrefs, path: Optional[Path] = None) -> None:
    """Atomically persist the pairing prefs."""
    save_toggle_prefs(
        prefs, Path(path) if path is not None else ALARM_PREFS_PATH, log_label="blind alarm pairing"
    )


def schedule_day_window(
    entries: Iterable[BlindScheduleEntry], weekday: str
) -> Optional[Tuple[str, str]]:
    """The blinds' own day on ``weekday``: earliest Up to latest Down, as HH:MM.

    Taken from the enabled schedule entries for that weekday. ``None`` when
    the schedule does not define both an Up and a later Down for it.
    """
    todays = [e for e in entries if e.enabled and weekday in (e.days or [])]
    ups = sorted(e.time for e in todays if e.action == "open")
    downs = sorted(e.time for e in todays if e.action == "close")
    if not ups or not downs or downs[-1] <= ups[0]:
        return None
    return ups[0], downs[-1]


def is_daytime(
    now_local: datetime,
    entries: Iterable[BlindScheduleEntry],
    sun_elevation_deg: Optional[float],
) -> Tuple[Optional[bool], str]:
    """Whether ``now_local`` is daytime for the blinds, and why.

    The blinds schedule defines the day when it can (see
    :func:`schedule_day_window`); otherwise the sun being above the horizon
    does. ``(None, why)`` when neither is known — unknown, never "day".
    """
    window = schedule_day_window(entries, now_local.strftime("%a").lower()[:3])
    if window is not None:
        start, end = window
        hhmm = now_local.strftime("%H:%M")
        inside = start <= hhmm < end
        return inside, f"schedule day {start}-{end}"
    if sun_elevation_deg is not None:
        return sun_elevation_deg > 0, f"sun elevation {sun_elevation_deg:.1f} deg"
    return None, "no schedule day and no home location"


def alarm_blind_action(
    kind: str, action: str, *, enabled: bool, daytime: Optional[bool]
) -> Tuple[Optional[str], str]:
    """The blind move a *confirmed* automatic alarm decision calls for.

    ``close`` when everyone left and the panel armed FULL; ``open`` when the
    first person arrived and the panel disarmed, during daytime only. A
    perimeter arm (the kids-home override) or a partial arm moves nothing —
    someone is inside. Returns ``(action, why)``; ``action`` is ``None`` for
    "leave the blinds alone".
    """
    if not enabled:
        return None, "pairing is off"
    if kind == "arm":
        if action == _FULL_ARM_ACTION:
            return "close", "everyone left, alarm armed full"
        return None, f"alarm armed {action}, not full"
    if kind == "disarm":
        if daytime is True:
            return "open", "first arrival, alarm disarmed in daytime"
        if daytime is False:
            return None, "first arrival at night"
        return None, "first arrival, daytime unknown"
    return None, f"no blind move for {kind}"
