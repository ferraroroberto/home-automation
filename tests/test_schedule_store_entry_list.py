"""The shared list-of-dataclass store helpers (issue #912).

Six stores (reminders, places, alarm schedules, scene pairings, overrides, wake
alarms) delegate their load/clean/save triple here; their own suites cover the
entry shapes, this one pins the generic mechanics with a throwaway dataclass.
Temp paths only — nothing here touches a real store.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from src import _schedule_store as S


@dataclass(frozen=True)
class Thing:
    id: str
    n: int = 0


def _clean(raw: dict, fallback_id: str) -> Optional[Thing]:
    if raw.get("reject"):
        return None
    return Thing(id=S.safe_id(raw.get("id"), fallback_id), n=int(raw.get("n") or 0))


def test_round_trip_writes_the_dataclass_fields(tmp_path):
    target = tmp_path / "things.json"
    S.save_entry_list(target, [Thing("a", 1), Thing("b", 2)])

    assert json.loads(target.read_text(encoding="utf-8")) == [
        {"id": "a", "n": 1},
        {"id": "b", "n": 2},
    ]
    assert S.load_entry_list(target, _clean, "thing") == [Thing("a", 1), Thing("b", 2)]


def test_missing_file_is_an_empty_list(tmp_path):
    assert S.load_entry_list(tmp_path / "nope.json", _clean, "thing") == []


def test_non_list_file_is_empty_and_warns_by_default(tmp_path, caplog):
    target = tmp_path / "things.json"
    target.write_text(json.dumps({"id": "a"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=S.logger.name):
        assert S.load_entry_list(target, _clean, "thing") == []

    assert "not a JSON list" in caplog.text


def test_non_list_file_can_be_read_silently(tmp_path, caplog):
    target = tmp_path / "things.json"
    target.write_text("42", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=S.logger.name):
        assert S.load_entry_list(target, _clean, "thing", warn_not_list=False) == []

    assert caplog.text == ""


def test_fallback_ids_count_every_item_and_skip_non_dicts(tmp_path):
    target = tmp_path / "things.json"
    target.write_text(json.dumps([{"n": 1}, "junk", {"n": 2}]), encoding="utf-8")

    assert [t.id for t in S.load_entry_list(target, _clean, "thing")] == ["thing-1", "thing-3"]


def test_a_cleaner_returning_none_drops_the_item(tmp_path):
    raw = [{"id": "a"}, {"id": "b", "reject": True}, {"id": "c"}]

    assert [t.id for t in S.clean_entry_list(raw, _clean, "thing")] == ["a", "c"]


def test_clean_entry_list_treats_a_non_list_as_empty():
    assert S.clean_entry_list({"id": "a"}, _clean, "thing") == []
    assert S.clean_entry_list(None, _clean, "thing") == []
