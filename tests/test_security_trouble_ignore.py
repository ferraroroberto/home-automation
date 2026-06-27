"""Unit tests for the per-detector trouble-ignore store (issue #225)."""

from __future__ import annotations

from pathlib import Path

from src.security_trouble_ignore import (
    load_ignored_trouble_zone_ids,
    set_zone_trouble_ignored,
)


def test_trouble_ignore_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "security_trouble_ignore.json"
    assert load_ignored_trouble_zone_ids(p) == set()

    set_zone_trouble_ignored("3", True, p)
    set_zone_trouble_ignored("7", True, p)
    assert load_ignored_trouble_zone_ids(p) == {"3", "7"}

    # Un-ignoring drops the entry; the rest remain.
    set_zone_trouble_ignored("3", False, p)
    assert load_ignored_trouble_zone_ids(p) == {"7"}

    # Un-ignoring an absent id is a no-op.
    set_zone_trouble_ignored("99", False, p)
    assert load_ignored_trouble_zone_ids(p) == {"7"}
