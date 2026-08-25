"""An unreadable store must report `unknown`, never `empty` (issue #689).

Every ``read_json`` caller in this repo is a read-modify-save store, so a read
that quietly degrades to the caller's default gets that default written back
over the real data on the very next save. That is how a transient Windows
sharing violation on ``config/presence_state.json`` erased the presence roster
and let auto-arm fire "everyone away" with someone asleep in the house.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import _schedule_store as S


def test_absent_file_still_returns_the_default(tmp_path):
    """The one case that legitimately means "nothing here yet"."""

    assert S.read_json(tmp_path / "nope.json", {"fallback": True}) == {"fallback": True}


def test_readable_file_round_trips(tmp_path):
    target = tmp_path / "store.json"
    target.write_text(json.dumps({"people": {"roberto": "home"}}), encoding="utf-8")
    assert S.read_json(target, {}) == {"people": {"roberto": "home"}}


def test_present_but_unreadable_raises_instead_of_returning_the_default(
    tmp_path, monkeypatch
):
    """The incident's exact shape: the file is right there, the read fails."""

    target = tmp_path / "store.json"
    target.write_text(json.dumps({"people": {"roberto": "home", "ana": "away"}}), encoding="utf-8")

    def _sharing_violation(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _sharing_violation)
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    with pytest.raises(S.StoreUnreadableError):
        S.read_json(target, {})


def test_corrupt_json_raises_rather_than_resetting_the_store(tmp_path, monkeypatch):
    """Truncated content is not an empty store — refusing loudly keeps the
    bytes on disk for a human instead of overwriting them with ``{}``."""

    target = tmp_path / "store.json"
    target.write_text('{"people": {"robert', encoding="utf-8")
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    with pytest.raises(S.StoreUnreadableError):
        S.read_json(target, {})


def test_a_transient_failure_is_retried_and_succeeds(tmp_path, monkeypatch):
    """The sharing violation lasts milliseconds — the retry is what turns the
    common case back into a plain successful read."""

    target = tmp_path / "store.json"
    payload = {"people": {"roberto": "home"}}
    target.write_text(json.dumps(payload), encoding="utf-8")

    real_read_text = Path.read_text
    attempts = {"n": 0}

    def _fails_once_then_works(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fails_once_then_works)
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    assert S.read_json(target, {}) == payload
    assert attempts["n"] == 2


def test_retries_are_bounded(tmp_path, monkeypatch):
    """A permanently unreadable file must not spin — it gives up and raises."""

    target = tmp_path / "store.json"
    target.write_text("{}", encoding="utf-8")
    attempts = {"n": 0}

    def _always_fails(self, *args, **kwargs):
        attempts["n"] += 1
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _always_fails)
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    with pytest.raises(S.StoreUnreadableError):
        S.read_json(target, {})
    assert attempts["n"] == S._READ_ATTEMPTS


def test_a_file_deleted_mid_retry_is_absent_not_unreadable(tmp_path, monkeypatch):
    """Losing the race to a legitimate delete is still just "nothing here"."""

    target = tmp_path / "store.json"
    target.write_text("{}", encoding="utf-8")

    def _fail_and_delete(self, *args, **kwargs):
        target.unlink(missing_ok=True)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _fail_and_delete)
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    assert S.read_json(target, {"fallback": True}) == {"fallback": True}
