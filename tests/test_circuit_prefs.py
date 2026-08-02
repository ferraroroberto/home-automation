"""Unit tests for the per-channel circuit prefs store (issue #25)."""

from __future__ import annotations

import json
from pathlib import Path

from src.circuit_prefs import (
    load_circuit_display_names,
    load_circuit_prefs,
    load_inverted_channels,
    set_circuit_display_name,
    set_circuit_inverted,
)

KEY = "AA:BB:CC:DD:EE:01:1"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_circuit_prefs(tmp_path / "nope.json") == {}
    assert load_circuit_display_names(tmp_path / "nope.json") == {}
    assert load_inverted_channels(tmp_path / "nope.json") == {}


def test_name_and_invert_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    set_circuit_display_name(KEY, "water heater", path)
    set_circuit_inverted(KEY, True, path)

    assert load_circuit_display_names(path) == {KEY: "water heater"}
    assert load_inverted_channels(path) == {KEY: True}


def test_setting_one_field_preserves_the_other(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    set_circuit_inverted(KEY, True, path)
    set_circuit_display_name(KEY, "termo", path)
    assert load_inverted_channels(path) == {KEY: True}
    set_circuit_display_name(KEY, "termo renamed", path)
    assert load_inverted_channels(path) == {KEY: True}


def test_clearing_everything_removes_the_row(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    set_circuit_display_name(KEY, "termo", path)
    set_circuit_inverted(KEY, True, path)
    set_circuit_display_name(KEY, "", path)
    assert load_circuit_prefs(path) == {KEY: {"display_name": "", "invert": True}}
    set_circuit_inverted(KEY, False, path)
    # Nothing left to remember — no empty husk left on disk.
    assert load_circuit_prefs(path) == {}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_only_inverted_channels_are_listed(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    set_circuit_display_name(KEY, "termo", path)
    other = "AA:BB:CC:DD:EE:01:2"
    set_circuit_inverted(other, True, path)
    assert load_inverted_channels(path) == {other: True}


def test_a_malformed_row_does_not_discard_the_others(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    path.write_text(
        json.dumps({KEY: {"display_name": "termo"}, "broken": "not an object"}),
        encoding="utf-8",
    )
    assert load_circuit_display_names(path) == {KEY: "termo"}


def test_unreadable_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_circuit_prefs(path) == {}


def test_names_are_stripped(tmp_path: Path) -> None:
    path = tmp_path / "circuit_prefs.json"
    set_circuit_display_name(KEY, "  termo  ", path)
    assert load_circuit_display_names(path) == {KEY: "termo"}
