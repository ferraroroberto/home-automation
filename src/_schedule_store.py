"""Shared read/clean helpers for weekly schedule-entry JSON stores (issue #327).

The ``_read_json`` / ``_save`` / ``_safe_id`` / ``_clean_time`` / ``_clean_days``
helpers below were duplicated verbatim across :mod:`src.wake_alarms` and
:mod:`src.security.security_schedules` (the read/save pair a third time in
:mod:`src.devices.hvac_automation`, per its README note that ``wake_alarms`` was
"cloned from security_schedules.py's atomic store"). Centralized here;
callers keep their own dataclass shape and entry-cleaning logic — only the
read/save mechanics, the list-of-entries load/save pair
(:func:`load_entry_list` / :func:`save_entry_list`, issue #912) and the
id/time/days field cleaners are shared, plus the due-window check
(:func:`daily_due`) and the persisted once-a-day fire latch
(:class:`DailyFireLatch`) the schedule engines fire through.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    TypeVar,
)

from src import activity_log
from src._atomic_json import write_json_atomic

logger = logging.getLogger(__name__)

T = TypeVar("T")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_SET = frozenset(DAYS)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A store that exists but momentarily can't be opened is nearly always another
# process's `os.replace` window — on Windows that surfaces as a sharing
# violation (`PermissionError`/`[Errno 13]`) lasting milliseconds. Retry a few
# times before concluding anything; the total stall is bounded well under a
# second so it's safe on the webapp's event loop, where every background tick
# reads these stores.
_READ_ATTEMPTS = 4
_READ_RETRY_DELAY_S = 0.05


class StoreUnreadableError(RuntimeError):
    """A store file exists but its contents could not be established.

    Deliberately *not* interchangeable with "the file isn't there" (issue
    #689). Every :func:`read_json` caller in this repo is a read-modify-save
    store, so handing back an empty default on a failed read means the next
    save persists that phantom empty over the real data. That is exactly how a
    transient sharing violation on ``config/presence_state.json`` erased the
    presence roster and let auto-arm fire "everyone away" on a household of
    two with one person asleep inside.

    Raising instead keeps the failure in its own state — unknown, not empty —
    so a background loop logs a failed tick and simply doesn't act, and nothing
    is written on top of data nobody could read.
    """


def read_json(path: Path, default: Any) -> Any:
    """Return parsed JSON from ``path``, or ``default`` if the file is absent.

    Raises :class:`StoreUnreadableError` when the file *exists* but cannot be
    read or parsed after :data:`_READ_ATTEMPTS` tries — an absent store and an
    unreadable one are different facts and must not share a return value.
    """

    last_exc: Exception | None = None
    for attempt in range(_READ_ATTEMPTS):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_exc = exc
        if attempt + 1 < _READ_ATTEMPTS:
            time.sleep(_READ_RETRY_DELAY_S)
    # Re-check rather than assume: a store legitimately deleted mid-retry is
    # absent, not unreadable, and still deserves the default.
    if not path.exists():
        return default
    logger.error(
        "❌ Could not read %s after %d attempts (%s) — refusing to treat it as empty",
        path,
        _READ_ATTEMPTS,
        last_exc,
    )
    raise StoreUnreadableError(f"could not read {path}: {last_exc}") from last_exc


def save_json(path: Path, data: Any) -> None:
    """Atomically persist ``data`` to ``path`` and log the standard save message."""

    write_json_atomic(path, data)
    logger.info("💾 Saved %s", path)


def clean_entry_list(
    raw: Any, clean: Callable[[dict, str], Optional[T]], id_prefix: str
) -> List[T]:
    """Run ``clean`` over every dict in ``raw``; anything else is dropped.

    ``clean`` gets the item and a ``f"{id_prefix}-{n}"`` fallback id (``n``
    counts from 1 over *all* items, so a skipped non-dict still consumes a
    number). A cleaner may return ``None`` to reject an item. A non-list
    ``raw`` yields ``[]``.
    """

    if not isinstance(raw, list):
        return []
    out: List[T] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        entry = clean(item, f"{id_prefix}-{idx}")
        if entry is not None:
            out.append(entry)
    return out


def load_entry_list(
    path: Path,
    clean: Callable[[dict, str], Optional[T]],
    id_prefix: str,
    *,
    warn_not_list: bool = True,
) -> List[T]:
    """Return the cleaned entry list persisted at ``path``, or ``[]`` if absent.

    A file holding something other than a JSON list also reads as ``[]``
    (logged unless ``warn_not_list`` is ``False``). An unreadable file still
    raises :class:`StoreUnreadableError` — see :func:`read_json`.
    """

    raw = read_json(path, [])
    if warn_not_list and not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", path)
    return clean_entry_list(raw, clean, id_prefix)


def save_entry_list(path: Path, entries: Sequence[Any]) -> None:
    """Atomically persist dataclass ``entries`` as a JSON list."""

    save_json(path, [asdict(entry) for entry in entries])


def safe_id(value: Any, fallback: str) -> str:
    """Sanitize an untrusted id into a DOM/key-safe slug, or ``fallback``."""

    raw = str(value or fallback).strip()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return safe or fallback


def clean_time(value: Any, default: str) -> str:
    """Coerce an untrusted value into a valid ``HH:MM`` string, or ``default``."""

    raw = str(value or default).strip()
    if not _TIME_RE.match(raw):
        return default
    hour, minute = (int(part) for part in raw.split(":", 1))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return default


def clean_date(value: Any) -> Optional[str]:
    """Coerce an untrusted value into a valid ``YYYY-MM-DD`` string, or ``None``."""

    raw = str(value or "").strip()
    if not raw or not _DATE_RE.match(raw):
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def clean_days(value: Any) -> List[str]:
    """Coerce an untrusted value into a de-duplicated list of valid day codes."""

    if not isinstance(value, list):
        return list(DAYS)
    seen: List[str] = []
    for item in value:
        day = str(item).strip().lower()[:3]
        if day in _DAY_SET and day not in seen:
            seen.append(day)
    return seen or list(DAYS)


def daily_due(
    time_hhmm: str, now: datetime, grace_s: int, days: Optional[List[str]] = None
) -> bool:
    """True when ``now`` falls in today's ``[HH:MM, HH:MM + grace)`` window.

    The HVAC schedule's narrow catch-up window, lifted out of
    ``app.webapp.automation`` so the blind schedule shares it (issue #871): a
    tick landing just past the minute still fires, but a restart hours later
    never replays a stale entry. ``days`` (``mon``..``sun``; ``None`` = every
    day) restricts which days it fires on. Deliberately no look-back past
    midnight: callers gate on "fired today" by local date, so a late-night
    entry caught after midnight would consume the next day's slot.
    """
    try:
        hour, minute = (int(part) for part in str(time_hhmm).split(":", 1))
        fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (TypeError, ValueError):
        logger.warning("⚠️ Invalid schedule time %r; skipping", time_hhmm)
        return False
    if days is not None and now.strftime("%a").lower()[:3] not in days:
        return False
    return 0 <= (now - fire_at).total_seconds() < grace_s


FIRE_SKIPPED = "skipped"
"""What a fire callable returns when it deliberately did not act (a presence
rule not met, say), so its engine's policy decides whether the day is spent."""


@dataclass(frozen=True)
class FireLatchPolicy:
    """How one schedule engine spends an entry's once-a-day fire.

    ``window`` names the engine's due window, ``"grace"`` (a few minutes past
    HH:MM) or ``"rest_of_day"`` (until midnight, so a failure keeps retrying);
    the engine's own due function enforces it. ``mark_before_fire`` spends the
    day before the fire is attempted, so a failure is not retried; otherwise
    only a fire that returns normally spends it. ``skip_consumes_day`` decides
    whether a fire that returns :data:`FIRE_SKIPPED` spends the day too.
    """

    name: str
    file_name: str
    window: Literal["grace", "rest_of_day"]
    mark_before_fire: bool = False
    skip_consumes_day: bool = False


class DailyFireLatch:
    """Entry key -> the local date it last fired, persisted under ``logs/``.

    Persisted so a tray restart that lands inside an entry's due window does
    not fire it a second time that day. The file is a flat
    ``{key: "YYYY-MM-DD"}`` object; a missing or unreadable file starts empty.
    """

    def __init__(self, policy: FireLatchPolicy) -> None:
        self.policy = policy
        self._days: Dict[str, str] = self._load()
        # Key -> the day its window was seen open without a fire yet, so a
        # window that closes unfired is logged exactly once.
        self._open: Dict[str, str] = {}

    @property
    def path(self) -> Path:
        # Read at call time so a redirected logs directory (tests) is honoured.
        return activity_log.LOGS_DIR / self.policy.file_name

    @property
    def days(self) -> Dict[str, str]:
        """A copy of the persisted key -> day map."""
        return dict(self._days)

    def _load(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("⚠️ %s fire state unreadable, starting empty: %s", self.policy.name, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, str)}

    def _save(self) -> None:
        try:
            write_json_atomic(self.path, self._days, indent=None)
        except OSError as exc:
            logger.warning("⚠️ Could not persist %s fire state: %s", self.policy.name, exc)

    def fired(self, key: str, day: str) -> bool:
        """True when ``key`` already fired on ``day``."""
        return self._days.get(key) == day

    def mark(self, key: str, day: str) -> None:
        """Spend ``day`` for ``key`` and persist. Entries for other days are
        dropped, since only the current day is ever compared."""
        self._days = {k: v for k, v in self._days.items() if v == day}
        self._days[key] = day
        self._save()

    def note_window(self, key: str, day: str, due: bool) -> None:
        """Track ``key``'s due window; log once when it closes unfired."""
        if due:
            if self.fired(key, day):
                self._open.pop(key, None)
            else:
                self._open.setdefault(key, day)
            return
        opened = self._open.pop(key, None)
        if opened is not None and not self.fired(key, opened):
            logger.info(
                "ℹ️ %s %s missed on %s: its %s window closed with no fire",
                self.policy.name, key, opened, self.policy.window,
            )

    async def fire_once(
        self, key: str, day: str, fire: Callable[[], Awaitable[Optional[str]]]
    ) -> bool:
        """Run ``fire`` and spend the day as the policy says.

        Returns ``True`` when ``fire`` acted, ``False`` when it returned
        :data:`FIRE_SKIPPED`. An exception from ``fire`` propagates, after the
        day is spent when the policy marks before firing.
        """
        if self.policy.mark_before_fire:
            self.mark(key, day)
        skipped = await fire() == FIRE_SKIPPED
        if not self.policy.mark_before_fire and (not skipped or self.policy.skip_consumes_day):
            self.mark(key, day)
        return not skipped
