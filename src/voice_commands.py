"""Curated catalogue of every wired Voice PE command (issue #437).

The spoken twin of ``docs/voice-pe-config/README.md``'s tables: the PWA's
"What can I say?" card renders this via ``GET /api/voice-commands``, so
remembering a command doesn't mean opening GitHub on a phone.

**Why curated, not derived from** ``docs/voice-pe-config/custom_sentences``:
those files are hassil templates (``"set [a] wake[-] [up] alarm for {when}"``),
so rendering them into readable examples means reimplementing hassil's
alternation/optional/wildcard expansion — and it still wouldn't be complete:
the multi-turn grocery flow is an ``assist_satellite.ask_question`` automation
in ``configuration.snippet.yaml``, and the HA built-ins aren't in this repo at
all. Curating is a fraction of the code and covers all three sources.

**The cost is a second place to update when wiring a new command** — paid down
by the checklist step in ``docs/voice-commands-howto.md``'s recipe, the same
anti-staleness contract as ``.fleet.toml`` / ``architecture.mmd``.

UI-free by convention (``src/`` never imports the webapp). This module holds
**no secrets**: the disarm code is only ever the ``<your code>`` placeholder
that ``voice-pe-config/README.md`` already publishes, never the value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

# Wake word is the language switch — a pipeline-level property in HA (STT
# hint, TTS voice, sentence matching). See docs/voice-commands-howto.md
# "Mixing English and Spanish".
WAKE_WORD_EN = "Okay Nabu"
WAKE_WORD_ES = "Hey Mycroft"


@dataclass(frozen=True)
class Phrasing:
    """One language's trigger phrases for a command.

    A command carries one ``Phrasing`` per pipeline it answers on: the family
    locator has both (#438 English, #446 Spanish), grocery is Spanish-only,
    the alarm is English-only.
    """

    lang: str
    wake_word: str
    phrases: Tuple[str, ...]
    example: str


@dataclass(frozen=True)
class VoiceCommand:
    id: str
    intent: str
    reply: str
    phrasings: Tuple[Phrasing, ...]


@dataclass(frozen=True)
class VoiceCommandGroup:
    id: str
    title: str
    icon: str
    summary: str
    commands: Tuple[VoiceCommand, ...]
    notes: Tuple[str, ...] = ()


# --------------------------------------------------------------- the catalogue
# Mirrors docs/voice-pe-config/README.md — keep the two in step (that file
# stays the source of truth for *what is deployed*; this one is what the phone
# shows). Phrase lists are the memorable subset, not the exhaustive hassil
# grammar: an unlisted phrasing may still match.

_ALARM = VoiceCommandGroup(
    id="alarm",
    title="Alarm",
    icon="shield-check",
    summary="The RISCO security system. Matched locally — no LLM can ever arm or disarm it.",
    commands=(
        VoiceCommand(
            id="alarm-arm",
            intent="Full arm",
            reply="Confirms the alarm is arming.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("alarm on", "full alarm on", "turn the alarm fully on", "fully arm", "activate the alarm"),
                    example="Okay Nabu, alarm on",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("arma la alarma", "activa la alarma", "alarma total", "arma la casa"),
                    example="Hey Mycroft, arma la alarma",
                ),
            ),
        ),
        VoiceCommand(
            id="alarm-perimeter",
            intent="Perimeter",
            reply="Confirms perimeter mode.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("perimeter on", "the perimeter on", "put the perimeter on", "perimeter mode"),
                    example="Okay Nabu, perimeter on",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("activa el perímetro", "pon el perímetro", "modo perímetro"),
                    example="Hey Mycroft, activa el perímetro",
                ),
            ),
        ),
        VoiceCommand(
            id="alarm-partial",
            intent="Partial",
            reply="Confirms partial mode.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("partial on", "partial alarm on", "arm partial", "partial mode"),
                    example="Okay Nabu, partial on",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("alarma parcial", "activa parcial", "modo parcial"),
                    example="Hey Mycroft, alarma parcial",
                ),
            ),
        ),
        VoiceCommand(
            id="alarm-status",
            intent="Status (read-only)",
            reply="Speaks the current alarm state.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=(
                        "what's the alarm status",
                        "what's the state of the alarm",
                        "is the alarm on",
                        "how is the alarm",
                    ),
                    example="Okay Nabu, what's the alarm status",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("¿cómo está la alarma?", "qué estado tiene la alarma", "está armada la alarma"),
                    example="Hey Mycroft, ¿cómo está la alarma?",
                ),
            ),
        ),
        VoiceCommand(
            id="alarm-disarm",
            intent="Disarm — needs your spoken code",
            reply="Disarms only if the spoken code matches; otherwise nothing happens.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("disarm <your code>", "turn off the alarm <your code>", "perimeter off <your code>"),
                    example="Okay Nabu, disarm <your code>",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("desarma la alarma <tu código>", "apaga la alarma <tu código>", "perímetro fuera <tu código>"),
                    example="Hey Mycroft, desarma la alarma <tu código>",
                ),
            ),
        ),
        VoiceCommand(
            id="alarm-disarm-no-code",
            intent="Disarm without a code",
            reply="Nothing is disarmed — it just tells you a code is needed.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("disarm", "alarm off", "perimeter off", "partial off"),
                    example="Okay Nabu, disarm",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("desarma", "apaga la alarma", "perímetro fuera"),
                    example="Hey Mycroft, desarma",
                ),
            ),
        ),
    ),
    notes=(
        "Say the code in the same breath as the command — a wrong or missing code never disarms.",
        "The spoken code is not the panel PIN: the real PIN stays server-side and is never spoken aloud.",
    ),
)

_WAKE_ALARMS = VoiceCommandGroup(
    id="wake-alarms",
    title="Wake alarms",
    icon="alarm-clock",
    summary="The alarms that ring on the Home tab. Every phrase says “wake alarm”, so it can never collide with the security alarm above.",
    commands=(
        VoiceCommand(
            id="wake-set",
            intent="Set an alarm",
            reply="Speaks back the time and schedule it understood.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=(
                        "set a wake alarm for 7 am",
                        "wake me up at half past six",
                        "set a wake-up alarm for 7 on weekdays",
                        "new wake alarm for 8 tomorrow",
                    ),
                    example="Okay Nabu, set a wake alarm for 7 am on weekdays",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=(
                        "pon una alarma para las 7",
                        "despiértame a las siete y media",
                        "pon una alarma para las 7 los fines de semana",
                        "alarma para las 8 mañana",
                    ),
                    example="Hey Mycroft, pon una alarma para las 7 entre semana",
                ),
            ),
        ),
        VoiceCommand(
            id="wake-cancel",
            intent="Cancel an alarm",
            reply="Cancels the soonest upcoming one — repeat for the next.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("cancel my wake alarm", "delete my wake alarms", "turn off my wake-up alarm"),
                    example="Okay Nabu, cancel my wake alarm",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("cancela mi alarma", "quita mis alarmas", "apaga mi alarma"),
                    example="Hey Mycroft, cancela mi alarma",
                ),
            ),
        ),
        VoiceCommand(
            id="wake-list",
            intent="List alarms (read-only)",
            reply="Speaks a summary of what's set.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("what wake alarms do I have", "list my wake alarms", "when are my wake alarms"),
                    example="Okay Nabu, what wake alarms do I have",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("qué alarmas tengo", "cuáles son mis alarmas", "cuándo suenan mis alarmas"),
                    example="Hey Mycroft, ¿qué alarmas tengo?",
                ),
            ),
        ),
    ),
    notes=(
        "Times it understands: 7 · 7 am · 7 pm · 7 30 · seven thirty · half past six · quarter to seven · noon. "
        "A bare number with no am/pm is taken as spoken (24-hour from 13 up, otherwise AM).",
        "Schedules it understands: on weekdays · on weekends · every day · a weekday name (on monday) all repeat; "
        "tomorrow / today make a one-shot that switches itself off after it rings. Say no schedule and it repeats every day.",
        "En español: «pon una alarma para las siete y media», «mañana a mediodía», «entre semana», «los fines de semana», "
        "«todos los días». Sin horario, suena todos los días.",
    ),
)

_LOCATOR = VoiceCommandGroup(
    id="locator",
    title="Family locator",
    icon="map-pin",
    summary="Read-only “where is…”, answered from already-cached Find My data. Ask in either language.",
    commands=(
        VoiceCommand(
            id="locate-who",
            intent="Locate someone",
            reply="Speaks the place they're at — a named place, home, or away — in the language you asked in. "
            "If they're away, it then offers to work out how long they'd take to get home.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("where's dad", "where is mom", "where's <name>", "locate <name>", "find dad"),
                    example="Okay Nabu, where's dad",
                ),
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("¿dónde está papá?", "donde esta mamá", "localiza a <nombre>", "encuentra a <nombre>"),
                    example="Hey Mycroft, ¿dónde está papá?",
                ),
            ),
        ),
    ),
    notes=(
        "Works by first name or household role. Nicknames, accents and kinship words all fold to the right person "
        "(mum/mamá → mom, daddy/papá → dad).",
        "Roles and named places are set on the Security tab's Presence card — no voice config to touch when someone new arrives.",
        "The “how long to get home?” follow-up only comes up when the person is away, and uses live traffic. If they're "
        "already home, it just says so.",
    ),
)

_GROCERY = VoiceCommandGroup(
    id="grocery",
    title="Grocery list",
    icon="sprout",
    summary="The shopping list, in Spanish only — it lives on the Spanish pipeline, so it always starts with “Hey Mycroft”.",
    commands=(
        VoiceCommand(
            id="grocery-add",
            intent="Add items",
            reply="Confirms what it added; anything it doesn't know becomes a new item.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("añade leche y dos huevos a la lista", "apunta <cosas>", "pon <cosas> en la lista"),
                    example="Hey Mycroft, añade leche y dos huevos a la lista",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-add-multiturn",
            intent="Add items, hands-free conversation",
            reply="The puck asks “¿Qué quieres añadir?” — answer in plain Spanish and it confirms.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("quiero añadir cosas a la lista",),
                    example="Hey Mycroft, quiero añadir cosas a la lista → “¿Qué quieres añadir?” → “leche y dos huevos”",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-target",
            intent="Set how many you want",
            reply="Confirms the new target.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("pon el objetivo de leche a cuatro", "objetivo de <cosa> a <número>"),
                    example="Hey Mycroft, pon el objetivo de leche a cuatro",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-stock",
            intent="Set how many you have",
            reply="Confirms the new count.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("anota que tenemos dos aceites", "tenemos <número> <cosa>"),
                    example="Hey Mycroft, anota que tenemos dos aceites",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-out",
            intent="Flag something as run out",
            reply="Sets the count to zero, so it lands on the list.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("no quedan huevos", "se acabó el pan"),
                    example="Hey Mycroft, no quedan huevos",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-query",
            intent="Read the list (read-only)",
            reply="Reads back what needs buying.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("qué hay que comprar", "lee la lista de la compra"),
                    example="Hey Mycroft, qué hay que comprar",
                ),
            ),
        ),
        VoiceCommand(
            id="grocery-help",
            intent="Ask it what it can do",
            reply="Speaks this menu out loud — the spoken twin of this card.",
            phrasings=(
                Phrasing(
                    lang="es",
                    wake_word=WAKE_WORD_ES,
                    phrases=("¿qué puedo hacer?", "opciones", "ayuda"),
                    example="Hey Mycroft, ¿qué puedo hacer?",
                ),
            ),
        ),
    ),
    notes=(
        "Ask in English and it won't answer: the wake word picks the language, and grocery only listens on the Spanish one.",
        "The list is worked out from the pantry counts — what to buy is what you want minus what you have.",
    ),
)

_BUILT_INS = VoiceCommandGroup(
    id="built-ins",
    title="Built in, no setup",
    icon="timer",
    summary="Home Assistant answers these on its own — nothing in this repo wires them.",
    commands=(
        VoiceCommand(
            id="builtin-timer",
            intent="Countdown timers",
            reply="Chimes on the puck you set it on when time is up.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("set a timer for 5 minutes", "cancel the timer", "how much time is left"),
                    example="Okay Nabu, set a timer for 5 minutes",
                ),
            ),
        ),
        VoiceCommand(
            id="builtin-time-weather",
            intent="Time and weather",
            reply="Speaks the answer.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("what time is it", "what's the date", "what's the weather"),
                    example="Okay Nabu, what's the weather",
                ),
            ),
        ),
        VoiceCommand(
            id="builtin-entities",
            intent="Switch things on and off",
            reply="Confirms it did.",
            phrasings=(
                Phrasing(
                    lang="en",
                    wake_word=WAKE_WORD_EN,
                    phrases=("turn on the kitchen light", "turn off the kitchen light"),
                    example="Okay Nabu, turn off the kitchen light",
                ),
            ),
        ),
    ),
    notes=(
        "Use whatever a thing is called in Home Assistant, and only for things exposed to Assist.",
        "These timers are separate from the wake alarms above: they live on the puck, are gone after they ring, "
        "and never show up on the Home tab. Use the Home tab's own timers for that.",
    ),
)

VOICE_COMMAND_GROUPS: Tuple[VoiceCommandGroup, ...] = (
    _ALARM,
    _WAKE_ALARMS,
    _LOCATOR,
    _GROCERY,
    _BUILT_INS,
)


def _jsonable(value: Any) -> Any:
    """Tuples -> lists, recursively.

    ``asdict`` rebuilds each container with its original type, so the catalogue's
    tuples survive as tuples. ``json``/FastAPI encode those as arrays anyway, but
    a function promising JSON-ready dicts shouldn't hand back a shape whose
    round-trip differs from what it returned.
    """

    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def load_voice_commands() -> List[Dict[str, Any]]:
    """The catalogue as JSON-ready dicts, in display order."""

    return [_jsonable(asdict(group)) for group in VOICE_COMMAND_GROUPS]
