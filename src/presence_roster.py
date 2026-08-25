"""The set of people presence automation knows it is supposed to be tracking (issue #689).

``config/presence_state.json`` holds each person's *current* state. It does not
record who is *expected* to have one, so a roster that silently shrank from two
people to one was indistinguishable from a genuine one-person household — and
"everyone away" became true with someone asleep in the building.

This store answers the missing question: every person id the engine has ever
seen. It is a union, never a subtraction — an id is added the first time a
webhook (or a tick that can see the live state) mentions it, and nothing in the
automation path removes one. A wiped or half-written state file therefore
*shrinks against the roster*, which the engine can see and refuse to act on.

Kept deliberately in its own file rather than as another key inside
``presence_state.json``: that file is rewritten by the per-tick block
diagnostics and is exactly the hot, contended file that got wiped. This one is
written only when the known set actually grows — a handful of times in the life
of a household — so it is nearly always a pure read.

Retiring someone (a person id that will never report again) is a hand edit of
``config/presence_roster.json``, the same affordance their entry in
``presence_state.json`` already has: there is no delete-person endpoint, and
adding one is not this store's job.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Tuple

from src._schedule_store import read_json, save_json

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "presence_roster.json"


def roster_path_for(state_path: Optional[Path]) -> Path:
    """Return the roster that belongs beside ``state_path``.

    Keying off the state file's directory (rather than taking a second path
    argument everywhere) means any caller already redirecting presence state to
    a temp dir — every test, and the worktree config copies — redirects the
    roster with it, and can never write the real household's file by accident.
    """

    return DEFAULT_PATH if state_path is None else Path(state_path).with_name(DEFAULT_PATH.name)


def load_roster(path: Optional[Path] = None) -> Tuple[str, ...]:
    """Return every known person id, sorted. Empty when the store is absent."""

    raw = read_json(DEFAULT_PATH if path is None else path, [])
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))


def remember_people(
    person_ids: Iterable[str], path: Optional[Path] = None
) -> Tuple[str, ...]:
    """Add ``person_ids`` to the roster, returning the full known set.

    A no-op — no write at all — when every id is already known, which is the
    overwhelmingly common case.
    """

    target = DEFAULT_PATH if path is None else path
    known = set(load_roster(target))
    fresh = {str(pid).strip() for pid in person_ids if str(pid).strip()} - known
    if not fresh:
        return tuple(sorted(known))
    known |= fresh
    save_json(target, sorted(known))
    logger.info("ℹ️ Presence roster now tracks %s", ", ".join(sorted(known)))
    return tuple(sorted(known))
