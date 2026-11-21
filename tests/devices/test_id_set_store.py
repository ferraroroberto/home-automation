"""The shared id-set store behind every hidden / ignored-trouble list."""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.devices.id_set_store import load_id_set, set_id_member

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_missing_file_is_an_empty_set(tmp_path: Path) -> None:
    assert load_id_set(tmp_path / "absent.json") == set()


def test_add_and_remove_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    set_id_member("3", True, path)
    set_id_member("7", True, path)
    assert load_id_set(path) == {"3", "7"}
    set_id_member("3", False, path)
    assert load_id_set(path) == {"7"}


def test_on_disk_shape_stays_the_id_to_one_map(tmp_path: Path) -> None:
    # Live gitignored files already hold this shape; changing it would orphan them.
    path = tmp_path / "ids.json"
    set_id_member("3", True, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"3": "1"}


def test_removing_an_absent_id_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    set_id_member("9", False, path)
    assert load_id_set(path) == set()


def test_no_store_re_declares_the_marker_or_stores_ids_as_display_names() -> None:
    """An id set goes through ``src.devices.id_set_store``, not a ``set_display_name`` call.

    Using the display-name API as a boolean-set store is what made five modules each
    declare their own ``"1"`` marker; this fails the next module that does it again.
    """
    marker = re.compile(r"^_[A-Z_]*MARKER\s*=", re.MULTILINE)
    offenders = []
    for root in ("src", "app"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            if marker.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"re-declared id-set marker (use src/devices/id_set_store.py): {offenders}"
