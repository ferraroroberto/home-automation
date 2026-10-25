"""An unreadable session store must raise, never fold into a fresh default (issue #692).

``security_override_session.py`` is a read-modify-save store: the automation
loads the session, mutates it (a trigger count, an auto-bypassed zone, the scan
cursor), and saves the whole thing back. Before this fix its own hand-rolled
reader degraded an unreadable file to a fresh :class:`OverrideSession` instead
of raising — the same shape issue #689 fixed for ``presence_state.json`` — so a
transient read failure got saved back over the real data on the very next
write, silently forgetting any zone this automation owed a restore.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src._schedule_store import StoreUnreadableError
from src.security_override_session import OverrideSession, load_override_session


def test_absent_store_returns_a_fresh_session(tmp_path) -> None:
    session = load_override_session(tmp_path / "nope.json")
    assert session == OverrideSession()


def test_readable_store_round_trips(tmp_path) -> None:
    target = tmp_path / "store.json"
    target.write_text(
        json.dumps(
            {
                "last_event_time": "2026-07-04T10:00:00Z",
                "session_counts": {"12": 1},
                "auto_bypassed_zones": [12],
            }
        ),
        encoding="utf-8",
    )
    session = load_override_session(target)
    assert session.last_event_time == "2026-07-04T10:00:00Z"
    assert session.session_counts == {"12": 1}
    assert session.auto_bypassed_zones == [12]


def test_present_but_unreadable_raises_instead_of_returning_a_fresh_session(
    tmp_path, monkeypatch
) -> None:
    """The incident's exact shape: the file is right there, the read fails —
    this must never come back looking like "nothing was ever bypassed"."""

    target = tmp_path / "store.json"
    target.write_text(
        json.dumps(
            {
                "last_event_time": "2026-07-04T10:00:00Z",
                "session_counts": {"12": 1},
                "auto_bypassed_zones": [12],
            }
        ),
        encoding="utf-8",
    )

    def _sharing_violation(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _sharing_violation)
    import src._schedule_store as store_mod

    monkeypatch.setattr(store_mod.time, "sleep", lambda _s: None)

    with pytest.raises(StoreUnreadableError):
        load_override_session(target)
