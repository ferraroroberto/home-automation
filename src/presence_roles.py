"""Local household-role aliases for presence people/entities (issue #438).

Maps a stable webhook person id or Find My entity id to a household role
(e.g. "dad", "mom") so "where's dad" and "where's Roberto" resolve to the same
person. Reuses :mod:`src.display_names`'s atomic id->string store verbatim —
the same pattern already followed by :mod:`src.presence_display_names`,
:mod:`src.tuya_display_names`, and :mod:`src.security_display_names`.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Dict, Iterable, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "presence_roles.json"


def _normalize(text: str) -> str:
    """Comparison key: lowercase, accents stripped, doubled letters collapsed.

    Whisper legitimately transcribes the same spoken word many ways ("Anna" for
    "Ana", "mamá" with or without the accent), so exact matching rejected
    correctly-heard queries (#446). Collapsing consecutive repeats makes
    "Anna" == "Ana"; the household scale (<10 people) makes collisions moot.
    """

    lowered = unicodedata.normalize("NFD", text.strip().lower())
    stripped = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    collapsed: list[str] = []
    for ch in stripped:
        if not collapsed or collapsed[-1] != ch:
            collapsed.append(ch)
    return "".join(collapsed)


# Spoken variants of the two household roles — generic English/Spanish kinship
# words only, never real names (the repo is public). Both the configured role
# and the spoken query are folded through this map, so "papá" as a stored role
# and "papa" as a query meet at the same canonical form.
_ROLE_VARIANTS: Dict[str, str] = {
    _normalize(variant): canonical
    for canonical, variants in {
        "mom": ("mum", "mummy", "mommy", "mama", "mamá", "mami"),
        "dad": ("daddy", "papa", "papá", "papi"),
    }.items()
    for variant in variants
}


def _canonical_role(text: str) -> str:
    key = _normalize(text)
    return _ROLE_VARIANTS.get(key, key)


def load_presence_roles(path: Optional[Path] = None) -> Dict[str, str]:
    """Return {person_or_entity_id: role}, or {} when absent."""
    return load_display_names(DEFAULT_PATH if path is None else path)


def set_presence_role(entity_id: str, role: str, path: Optional[Path] = None) -> None:
    """Set or clear one presence role alias."""
    set_display_name(entity_id, role, DEFAULT_PATH if path is None else path)


def resolve_person(
    who: str,
    *,
    roles: Dict[str, str],
    display_names: Dict[str, str],
    known_ids: Iterable[str],
    known_names: Dict[str, str],
) -> Optional[str]:
    """Match a spoken ``who`` fragment to a person/entity id.

    Checks role aliases, display-name overrides, and raw entity/person names —
    in that order, so a configured role or custom label always wins over
    whatever the underlying source calls the entity. Matching is tolerant of
    transcription variants (see :func:`_normalize`), and role matching also
    folds common kinship synonyms ("mum"/"mamá" → "mom", "daddy"/"papá" →
    "dad") so the deterministic variant table — not fuzzy matching — absorbs
    how Whisper actually spells what it heard (#446).
    """

    needle = _normalize(who)
    if not needle:
        return None
    needle_role = _ROLE_VARIANTS.get(needle, needle)

    for entity_id, role in roles.items():
        if _canonical_role(role) == needle_role:
            return entity_id
    for entity_id, name in display_names.items():
        if _normalize(name) == needle:
            return entity_id
    for entity_id in known_ids:
        name = known_names.get(entity_id, "")
        if _normalize(name) == needle or _normalize(entity_id) == needle:
            return entity_id
    return None
