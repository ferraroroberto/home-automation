"""Persisted wake-alarm entries (issue #304).

The browser edits a single list of entries. The webapp-owned background task
(``app.webapp.wake_alarm_automation``) loads that same list, fires due
entries, and rearms (weekly) or auto-disables (one-shot) them afterward.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src._schedule_store import clean_days, clean_time, read_json, safe_id, save_json

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
WAKE_ALARMS_PATH = _CONFIG_DIR / "wake_alarms.json"

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")
_WEEKEND = ("sat", "sun")
_DAY_FULL = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


@dataclass(frozen=True)
class WakeAlarmEntry:
    """One wake-alarm entry — recurring (``days``) or one-shot (``date``).

    ``date`` takes precedence when set: the alarm fires once on that local
    date, then the caller (the background loop) disables it. Otherwise it
    recurs weekly on ``days`` (defaults to every day when empty).
    """

    id: str
    label: str = ""
    enabled: bool = True
    time: str = "07:00"
    days: List[str] | None = None
    date: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "days", list(self.days or DAYS))


def _clean_date(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or not _DATE_RE.match(raw):
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def clean_entry(raw: dict, fallback_id: str) -> WakeAlarmEntry:
    """Coerce untrusted JSON/API data into a wake-alarm entry."""

    return WakeAlarmEntry(
        id=safe_id(raw.get("id"), fallback_id),
        label=str(raw.get("label") or "").strip()[:80],
        enabled=raw.get("enabled") is not False,
        time=clean_time(raw.get("time"), "07:00"),
        days=clean_days(raw.get("days")),
        date=_clean_date(raw.get("date")),
    )


def load_wake_alarms(path: Optional[Path] = None) -> List[WakeAlarmEntry]:
    """Return the persisted wake-alarm list, or ``[]`` if absent."""

    target = Path(path) if path is not None else WAKE_ALARMS_PATH
    raw = read_json(target, [])
    if not isinstance(raw, list):
        logger.warning("⚠️ %s is not a JSON list; returning empty", target)
        return []
    return [
        clean_entry(item, f"alarm-{idx}")
        for idx, item in enumerate(raw, start=1)
        if isinstance(item, dict)
    ]


def save_wake_alarms(entries: List[WakeAlarmEntry], path: Optional[Path] = None) -> None:
    """Atomically persist the whole wake-alarm list."""

    target = Path(path) if path is not None else WAKE_ALARMS_PATH
    save_json(target, [asdict(entry) for entry in entries])


def set_wake_alarms(raw_entries: List[dict], path: Optional[Path] = None) -> List[WakeAlarmEntry]:
    """Replace the wake-alarm list with normalized entries and return it."""

    entries = [
        clean_entry(item, f"alarm-{idx}")
        for idx, item in enumerate(raw_entries, start=1)
        if isinstance(item, dict)
    ]
    save_wake_alarms(entries, path)
    return entries


def wake_alarm_due(entry: WakeAlarmEntry, now: datetime, grace_s: int) -> bool:
    """True when ``now`` is inside this entry's local fire window."""

    hour, minute = (int(part) for part in entry.time.split(":", 1))
    candidate_days = (now, now - timedelta(days=1))
    if entry.date:
        for schedule_day in candidate_days:
            if schedule_day.strftime("%Y-%m-%d") != entry.date:
                continue
            fire_at = schedule_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - fire_at).total_seconds()
            if 0 <= delta < grace_s:
                return True
        return False
    days = set(entry.days or [])
    for schedule_day in candidate_days:
        if schedule_day.strftime("%a").lower()[:3] not in days:
            continue
        fire_at = schedule_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now - fire_at).total_seconds()
        if 0 <= delta < grace_s:
            return True
    return False


# --------------------------------------------------------------------------- #
# Next-occurrence helpers (voice "cancel my wake alarm" targets the soonest)
# --------------------------------------------------------------------------- #
def next_fire(entry: WakeAlarmEntry, now: datetime) -> datetime:
    """Return the next local datetime this entry fires (its date/time for a
    one-shot; the next matching weekday-at-time for a recurring entry)."""

    hour, minute = (int(part) for part in entry.time.split(":", 1))
    if entry.date:
        base = datetime.strptime(entry.date, "%Y-%m-%d")
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return cand if cand >= now else datetime.max
    days = set(entry.days or DAYS)
    for offset in range(8):
        cand = (now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if cand.strftime("%a").lower()[:3] in days and cand >= now:
            return cand
    return now  # unreachable for a well-formed entry, but never raise


def soonest_enabled(
    entries: List[WakeAlarmEntry], now: datetime
) -> Optional[WakeAlarmEntry]:
    """The enabled entry that fires next, or ``None`` when none are enabled."""

    enabled = [entry for entry in entries if entry.enabled]
    if not enabled:
        return None
    return min(enabled, key=lambda entry: next_fire(entry, now))


def describe_alarm(entry: WakeAlarmEntry, lang: str = "en") -> str:
    """A short, speakable description, e.g. ``"7 AM on weekdays"``.

    ``lang="es"`` returns the Spanish equivalent ("las 7 de la mañana entre
    semana") for the "Asistente (es)" pipeline; anything else is English.
    """

    if lang == "es":
        return _describe_alarm_es(entry)

    hour, minute = (int(part) for part in entry.time.split(":", 1))
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    clock = f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"

    if entry.date:
        day = datetime.strptime(entry.date, "%Y-%m-%d")
        return f"{clock} on {day.strftime('%A')} {day.strftime('%B')} {day.day}"

    days = list(entry.days or DAYS)
    if len(days) == 7:
        return f"{clock} every day"
    if tuple(day for day in DAYS if day in days) == _WEEKDAYS:
        return f"{clock} on weekdays"
    if tuple(day for day in DAYS if day in days) == _WEEKEND:
        return f"{clock} on weekends"
    names = [_DAY_FULL[day] for day in DAYS if day in days]
    return f"{clock} on {', '.join(names)}"


# --------------------------------------------------------------------------- #
# Spanish descriptions ("las 7 de la mañana entre semana") — #466
# --------------------------------------------------------------------------- #
_DAY_FULL_ES = {
    "mon": "lunes", "tue": "martes", "wed": "miércoles", "thu": "jueves",
    "fri": "viernes", "sat": "sábado", "sun": "domingo",
}
_MONTH_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
)


def _clock_es(hour: int, minute: int) -> str:
    """Spanish spoken clock, e.g. ``"la 1 de la madrugada"`` / ``"las 7 y media
    de la tarde"`` / ``"las 8 menos cuarto de la tarde"`` (:45 → menos cuarto of
    the following hour, as Spanish is spoken). The day-part follows the real
    hour, so 07:45 stays "de la mañana"."""

    if minute == 45:
        # "menos cuarto" names the following hour: 19:45 → "las 8 menos cuarto".
        spoken_h = (hour + 1) % 24
        mins = " menos cuarto"
    else:
        spoken_h = hour
        if minute == 0:
            mins = ""
        elif minute == 15:
            mins = " y cuarto"
        elif minute == 30:
            mins = " y media"
        else:
            mins = f" y {minute}"
    h12 = spoken_h % 12 or 12
    article = "la" if h12 == 1 else "las"
    if hour == 0:
        daypart = "de la noche"
    elif hour < 6:
        daypart = "de la madrugada"
    elif hour < 12:
        daypart = "de la mañana"
    elif hour == 12:
        daypart = "del mediodía"
    elif hour < 21:
        daypart = "de la tarde"
    else:
        daypart = "de la noche"
    return f"{article} {h12}{mins} {daypart}"


def _describe_alarm_es(entry: WakeAlarmEntry) -> str:
    hour, minute = (int(part) for part in entry.time.split(":", 1))
    clock = _clock_es(hour, minute)

    if entry.date:
        day = datetime.strptime(entry.date, "%Y-%m-%d")
        return (
            f"{clock} el {_DAY_FULL_ES[day.strftime('%a').lower()[:3]]} "
            f"{day.day} de {_MONTH_ES[day.month - 1]}"
        )

    days = list(entry.days or DAYS)
    if len(days) == 7:
        return f"{clock} todos los días"
    if tuple(day for day in DAYS if day in days) == _WEEKDAYS:
        return f"{clock} entre semana"
    if tuple(day for day in DAYS if day in days) == _WEEKEND:
        return f"{clock} los fines de semana"
    names = [_DAY_FULL_ES[day] for day in DAYS if day in days]
    if len(names) == 1:
        return f"{clock} los {names[0]}"
    return f"{clock} los {', '.join(names[:-1])} y {names[-1]}"


# --------------------------------------------------------------------------- #
# Spoken-phrase parsing ("7 am on weekdays", "half past six tomorrow", …)
# --------------------------------------------------------------------------- #
_WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "midnight": 0, "noon": 12,
}
_WEEKDAY_WORDS = {
    "mon": "mon", "monday": "mon",
    "tue": "tue", "tues": "tue", "tuesday": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed",
    "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu",
    "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}


def _word_or_int(token: str) -> Optional[int]:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_HOURS.get(token)


def _fmt_time(hour: int, minute: int, ampm: Optional[str]) -> Optional[str]:
    if ampm == "p" and hour < 12:
        hour += 12
    elif ampm == "a" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _parse_time(text: str) -> Optional[str]:
    """Best-effort HH:MM from a spoken time fragment; ``None`` if none found."""

    ampm: Optional[str] = None
    m = re.search(r"\b([ap])\.?\s*\.?m\.?\b", text)
    if m:
        ampm = m.group(1)
        text = text[: m.start()] + " " + text[m.end():]

    m = re.search(r"\bhalf past (\w+)\b", text)
    if m and (hour := _word_or_int(m.group(1))) is not None:
        return _fmt_time(hour, 30, ampm)
    m = re.search(r"\bquarter past (\w+)\b", text)
    if m and (hour := _word_or_int(m.group(1))) is not None:
        return _fmt_time(hour, 15, ampm)
    m = re.search(r"\bquarter to (\w+)\b", text)
    if m and (hour := _word_or_int(m.group(1))) is not None:
        return _fmt_time((hour - 1) % 24, 45, ampm)
    m = re.search(r"\b(\w+)\s*o'?clock\b", text)
    if m and (hour := _word_or_int(m.group(1))) is not None:
        return _fmt_time(hour, 0, ampm)
    m = re.search(r"\b(\w+)\s+(fifteen|thirty|forty[\s-]?five)\b", text)
    if m and (hour := _word_or_int(m.group(1))) is not None:
        minute = {"fifteen": 15, "thirty": 30}.get(m.group(2), 45)
        return _fmt_time(hour, minute, ampm)
    m = re.search(r"\b(\d{1,2})[:\s](\d{2})\b", text)
    if m:
        return _fmt_time(int(m.group(1)), int(m.group(2)), ampm)
    m = re.search(r"\b(\d{1,2})\b", text)
    if m:
        return _fmt_time(int(m.group(1)), 0, ampm)
    for word, hour in _WORD_HOURS.items():
        if re.search(rf"\b{word}\b", text):
            return _fmt_time(hour, 0, ampm)
    return None


# --------------------------------------------------------------------------- #
# Spanish spoken-phrase parsing ("las siete de la mañana entre semana") — #466
# --------------------------------------------------------------------------- #
_WORD_HOURS_ES = {
    "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
    "doce": 12, "mediodia": 12, "medianoche": 0,
}
_WEEKDAY_WORDS_ES = {
    "lunes": "mon", "martes": "tue", "miercoles": "wed", "jueves": "thu",
    "viernes": "fri", "sabado": "sat", "domingo": "sun",
}


def _strip_accents(text: str) -> str:
    """Fold accents/ñ so Spanish keyword matching is transcription-tolerant
    ("mañana"→"manana", "miércoles"→"miercoles")."""

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _word_or_int_es(token: str) -> Optional[int]:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _WORD_HOURS_ES.get(token)


def _parse_time_es(text: str) -> Optional[str]:
    """Best-effort HH:MM from a Spanish spoken time fragment (accents already
    folded by the caller); ``None`` if none found."""

    ampm: Optional[str] = None
    if re.search(r"de la (manana|madrugada)", text):
        ampm = "a"
        text = re.sub(r"de la (manana|madrugada)", " ", text)
    elif re.search(r"de la (tarde|noche)|del mediodia", text):
        ampm = "p"
        text = re.sub(r"de la (tarde|noche)", " ", text)

    if re.search(r"\bmedianoche\b", text):
        return "00:00"
    if re.search(r"\bmediodia\b", text):
        return "12:00"

    hour_word = r"(?:la |las )?(\w+)"
    m = re.search(rf"\b{hour_word}\s+menos\s+cuarto\b", text)
    if m and (hour := _word_or_int_es(m.group(1))) is not None:
        return _fmt_time((hour - 1) % 24, 45, ampm)
    m = re.search(rf"\b{hour_word}\s+y\s+media\b", text)
    if m and (hour := _word_or_int_es(m.group(1))) is not None:
        return _fmt_time(hour, 30, ampm)
    m = re.search(rf"\b{hour_word}\s+y\s+cuarto\b", text)
    if m and (hour := _word_or_int_es(m.group(1))) is not None:
        return _fmt_time(hour, 15, ampm)
    m = re.search(rf"\b{hour_word}\s+y\s+(\d{{1,2}})\b", text)
    if m and (hour := _word_or_int_es(m.group(1))) is not None:
        return _fmt_time(hour, int(m.group(2)), ampm)
    m = re.search(r"\b(\d{1,2})[:\s](\d{2})\b", text)
    if m:
        return _fmt_time(int(m.group(1)), int(m.group(2)), ampm)
    m = re.search(r"\b(?:la |las )(\w+)\b", text)
    if m and (hour := _word_or_int_es(m.group(1))) is not None:
        return _fmt_time(hour, 0, ampm)
    m = re.search(r"\b(\d{1,2})\b", text)
    if m:
        return _fmt_time(int(m.group(1)), 0, ampm)
    for word, hour in _WORD_HOURS_ES.items():
        if re.search(rf"\b{word}\b", text):
            return _fmt_time(hour, 0, ampm)
    return None


def _parse_spoken_alarm_es(phrase: str, now: datetime) -> Optional[Dict[str, Any]]:
    text = _strip_accents(" ".join(str(phrase or "").lower().split()))
    days: Optional[List[str]] = None
    date: Optional[str] = None

    if re.search(r"\bentre semana\b", text):
        days = list(_WEEKDAYS)
        text = re.sub(r"\bentre semana\b", " ", text)
    elif re.search(r"\b(los |el )?fines? de semana\b", text):
        days = list(_WEEKEND)
        text = re.sub(r"\b(los |el )?fines? de semana\b", " ", text)
    elif re.search(r"\b(todos los dias|cada dia|a diario|diariamente)\b", text):
        days = list(DAYS)
        text = re.sub(r"\b(todos los dias|cada dia|a diario|diariamente)\b", " ", text)

    # "de la mañana" is an am marker (stripped inside _parse_time_es); a
    # *standalone* "mañana" means tomorrow — so only treat it as a date when it
    # is not part of the "de la mañana" day-part.
    if re.search(r"\bpasado manana\b", text):
        date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        text = re.sub(r"\bpasado manana\b", " ", text)
    elif re.search(r"\bmanana\b", text) and not re.search(r"de la manana", text):
        date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        text = re.sub(r"\bmanana\b", " ", text)
    elif re.search(r"\b(hoy|esta noche)\b", text):
        date = now.strftime("%Y-%m-%d")
        text = re.sub(r"\b(hoy|esta noche)\b", " ", text)

    if days is None and date is None:
        found = [
            _WEEKDAY_WORDS_ES[tok] for tok in text.split() if tok in _WEEKDAY_WORDS_ES
        ]
        if found:
            days = [day for day in DAYS if day in set(found)]
            text = " ".join(t for t in text.split() if t not in _WEEKDAY_WORDS_ES)

    time_str = _parse_time_es(text)
    if time_str is None:
        return None

    return {
        "label": "Despertar",
        "enabled": True,
        "time": time_str,
        "days": days if days is not None else list(DAYS),
        "date": date,
    }


def parse_spoken_alarm(
    phrase: str, now: datetime, lang: str = "en"
) -> Optional[Dict[str, Any]]:
    """Turn a spoken fragment into a raw wake-alarm dict, or ``None`` on no time.

    Returns a dict shaped for :func:`clean_entry` (no ``id`` — the caller
    assigns a stable one). Recognises a clock time plus an optional schedule:
    ``tomorrow``/``today`` (one-shot), ``weekdays``/``weekends``/``every day``,
    or a weekday name (recurring). Unscheduled → every day at that time.

    ``lang="es"`` parses the Spanish equivalents ("las siete de la mañana entre
    semana", "mañana a mediodía") for the "Asistente (es)" pipeline.
    """

    if lang == "es":
        return _parse_spoken_alarm_es(phrase, now)

    text = " ".join(str(phrase or "").lower().split())
    days: Optional[List[str]] = None
    date: Optional[str] = None

    if re.search(r"\bweek ?days?\b", text):
        days = list(_WEEKDAYS)
        text = re.sub(r"\b(on )?week ?days?\b", " ", text)
    elif re.search(r"\bweek ?ends?\b", text):
        days = list(_WEEKEND)
        text = re.sub(r"\b(on )?week ?ends?\b", " ", text)
    elif re.search(r"\b(every ?day|everyday|daily|each day)\b", text):
        days = list(DAYS)
        text = re.sub(r"\b(every ?day|everyday|daily|each day)\b", " ", text)

    if re.search(r"\btomorrow\b", text):
        date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        text = re.sub(r"\btomorrow\b", " ", text)
    elif re.search(r"\btonight\b", text):
        date = now.strftime("%Y-%m-%d")
        text = re.sub(r"\btonight\b", " ", text)
    elif re.search(r"\btoday\b", text):
        date = now.strftime("%Y-%m-%d")
        text = re.sub(r"\btoday\b", " ", text)

    if days is None and date is None:
        found = [
            _WEEKDAY_WORDS[tok]
            for tok in text.split()
            if tok in _WEEKDAY_WORDS
        ]
        if found:
            days = [day for day in DAYS if day in set(found)]
            text = " ".join(t for t in text.split() if t not in _WEEKDAY_WORDS)

    time_str = _parse_time(text)
    if time_str is None:
        return None

    return {
        "label": "Wake up",
        "enabled": True,
        "time": time_str,
        "days": days if days is not None else list(DAYS),
        "date": date,
    }
