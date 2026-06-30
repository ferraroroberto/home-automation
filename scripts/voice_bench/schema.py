"""The Tier-2 intent contract: a fixed enum of intents + typed slots.

This is the schema Python would validate a model's output against (and
match against an allow-list) before any actuation. It is deliberately
small and closed-set — Tier-2 is classification, not open tool-calling.

Two consumers:
- ``INTENT_JSON_SCHEMA`` drives grammar-constrained decoding (llama.cpp
  ``response_format: {type: json_schema}``) and ``jsonschema`` validation.
- ``SYSTEM_PROMPT`` is the terse single-shot instruction given to every
  model, listing the same enum so a free (un-grammared) model has a fair
  chance of emitting valid output too.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Closed-set intents this app can actuate (HVAC + plugs + alarm + media),
# plus read-only status and the Tier-3 escape hatch.
INTENTS: List[str] = [
    "hvac_power",            # slots: area, state
    "hvac_set_temperature",  # slots: area, temperature
    "hvac_set_mode",         # slots: area, mode
    "hvac_set_fan",          # slots: area, fan_speed
    "plug_power",            # slots: device, state
    "alarm_arm",             # slots: arm_mode
    "alarm_disarm",          # slots: (code handled out-of-band)
    "media_volume",          # slots: direction and/or value
    "query_status",          # slots: target, area?
    "freeform",              # open question — route to Tier 3
    "unknown",               # not classifiable / out of scope
]

STATES: List[str] = ["on", "off"]
HVAC_MODES: List[str] = ["heat", "cool", "dry", "fan", "auto"]
FAN_SPEEDS: List[str] = ["auto", "low", "medium", "high"]
ARM_MODES: List[str] = ["full", "perimeter", "partial"]
STATUS_TARGETS: List[str] = ["hvac", "energy", "alarm", "plug"]
DIRECTIONS: List[str] = ["up", "down"]

# JSON Schema for the structured output. additionalProperties:false on both
# levels keeps a model from inventing fields; every slot is optional because
# which slots are relevant depends on the intent (cross-field conditionals
# are intentionally left to the Python allow-list, not encoded here, to keep
# the GBNF grammar small and fast).
INTENT_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent"],
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "slots": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "area": {"type": "string"},
                "device": {"type": "string"},
                "state": {"type": "string", "enum": STATES},
                "temperature": {"type": "number"},
                "mode": {"type": "string", "enum": HVAC_MODES},
                "fan_speed": {"type": "string", "enum": FAN_SPEEDS},
                "arm_mode": {"type": "string", "enum": ARM_MODES},
                "target": {"type": "string", "enum": STATUS_TARGETS},
                "direction": {"type": "string", "enum": DIRECTIONS},
                "value": {"type": "number"},
            },
        },
    },
}

# Wrapper shape llama.cpp / OpenAI expect for response_format.
RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "voice_intent", "strict": True, "schema": INTENT_JSON_SCHEMA},
}

SYSTEM_PROMPT = (
    "You are the intent classifier for a home voice assistant. Classify the "
    "user's single utterance into exactly one intent and its slots, and reply "
    "with ONLY a JSON object — no prose, no markdown, no explanation.\n\n"
    f"intent must be one of: {', '.join(INTENTS)}.\n"
    "Slots (include only those present in the utterance):\n"
    f"  area: a room name (free text)\n"
    f"  device: a plug/appliance name (free text)\n"
    f"  state: one of {STATES}\n"
    f"  temperature: a number in Celsius\n"
    f"  mode: one of {HVAC_MODES}\n"
    f"  fan_speed: one of {FAN_SPEEDS}\n"
    f"  arm_mode: one of {ARM_MODES}\n"
    f"  target: one of {STATUS_TARGETS} (for query_status)\n"
    f"  direction: one of {DIRECTIONS} (for media_volume)\n"
    f"  value: a number (e.g. a volume level)\n\n"
    "Use freeform for open questions (weather, facts, chit-chat). Use unknown "
    "if it is not a home command or question. Output JSON only."
)
