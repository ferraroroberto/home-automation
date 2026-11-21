"""A persisted set of ids (hidden rows, ignored troubles), one JSON file per set.

The on-disk shape is a ``{id: "1"}`` map: membership is the key's presence, and
the ``"1"`` value only exists because the atomic map helpers shared with the
display-name stores drop falsy values. Keeping that shape means every existing
live file keeps loading, and the real files stay gitignored (they would expose
which devices exist in a public repo). A missing file is an empty set, not an
error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

from src.devices.display_names import load_display_names, save_display_names

# The value stored under each member id; any truthy string keeps the entry.
_MEMBER_VALUE = "1"


def load_id_set(path: Path) -> Set[str]:
    """Return the ids stored at ``path``, or an empty set if the file is absent."""
    return set(load_display_names(path))


def set_id_member(member_id: str, member: bool, path: Path) -> None:
    """Add (``member=True``) or remove one id, persisting immediately."""
    ids = load_display_names(path)
    if member:
        ids[member_id] = _MEMBER_VALUE
    else:
        ids.pop(member_id, None)
    save_display_names(ids, path)
