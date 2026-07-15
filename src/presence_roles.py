"""Local household-role aliases for presence people/entities (issue #438).

Maps a stable webhook person id or Find My entity id to a household role
(e.g. "dad", "mom") so "where's dad" and "where's Roberto" resolve to the same
person. Reuses :mod:`src.display_names`'s atomic id->string store verbatim —
the same pattern already followed by :mod:`src.presence_display_names`,
:mod:`src.tuya_display_names`, and :mod:`src.security_display_names`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional

from src.display_names import load_display_names, set_display_name

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "presence_roles.json"


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

    Checks, case-insensitively: role aliases, display-name overrides, and raw
    entity/person names — in that order, so a configured role or custom label
    always wins over whatever the underlying source calls the entity.
    """

    needle = who.strip().lower()
    if not needle:
        return None

    for entity_id, role in roles.items():
        if role.strip().lower() == needle:
            return entity_id
    for entity_id, name in display_names.items():
        if name.strip().lower() == needle:
            return entity_id
    for entity_id in known_ids:
        name = known_names.get(entity_id, "")
        if name.strip().lower() == needle or entity_id.strip().lower() == needle:
            return entity_id
    return None
