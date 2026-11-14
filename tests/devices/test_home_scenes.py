"""The Home scenes store: built-in defaults, per-field overrides, bad values ignored."""

from __future__ import annotations

import json
from pathlib import Path

from src.devices.home_scenes import DEFAULT_SCENES, SCENE_IDS, get_scene, load_scenes


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "home_scenes.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_absent_file_gives_the_defaults_in_display_order(tmp_path: Path) -> None:
    scenes = load_scenes(tmp_path / "missing.json")
    assert [s.id for s in scenes] == list(SCENE_IDS)
    assert scenes == [DEFAULT_SCENES[i] for i in SCENE_IDS]
    assert scenes[0].step_labels() == ["Blinds down", "AC off", "Alarm full"]


def test_override_replaces_only_the_named_fields(tmp_path: Path) -> None:
    path = _write(tmp_path, {"night": {"alarm": "partial", "ac_off": True, "label": "Bed"}})
    night = get_scene("night", path)
    assert night is not None
    assert (night.label, night.blinds, night.ac_off, night.alarm) == ("Bed", "close", True, "partial")
    assert get_scene("leave", path) == DEFAULT_SCENES["leave"]


def test_null_clears_a_step(tmp_path: Path) -> None:
    path = _write(tmp_path, {"arrive": {"blinds": None}})
    arrive = get_scene("arrive", path)
    assert arrive is not None and arrive.blinds is None
    assert arrive.step_labels() == ["Alarm off"]


def test_invalid_values_keep_the_default(tmp_path: Path) -> None:
    path = _write(tmp_path, {"leave": {"alarm": "explode", "blinds": "sideways", "ac_off": "yes"}})
    assert get_scene("leave", path) == DEFAULT_SCENES["leave"]


def test_malformed_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "home_scenes.json"
    path.write_text("[1, 2", encoding="utf-8")
    assert load_scenes(path) == [DEFAULT_SCENES[i] for i in SCENE_IDS]
    assert load_scenes(_write(tmp_path, ["not", "an", "object"])) == [DEFAULT_SCENES[i] for i in SCENE_IDS]


def test_unknown_scene_is_none(tmp_path: Path) -> None:
    assert get_scene("party", tmp_path / "missing.json") is None


def test_sample_config_spells_out_the_defaults() -> None:
    sample = Path(__file__).resolve().parents[2] / "config" / "home_scenes.sample.json"
    assert load_scenes(sample) == [DEFAULT_SCENES[i] for i in SCENE_IDS]
