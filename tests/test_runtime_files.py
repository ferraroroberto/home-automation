"""The guarded writer refuses code-folder paths and passes runtime ones."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from src import runtime_files
from src.runtime_files import (
    CODE_FOLDERS,
    REPO_ROOT,
    RuntimeWriteRefused,
    ensure_runtime_path,
    make_dirs,
    open_runtime,
    write_bytes,
    write_text,
)


def test_code_folders_match_the_ratchet_config() -> None:
    config = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert tuple(config["runtime_write_ratchet"]["code_folders"]) == CODE_FOLDERS


@pytest.mark.parametrize("folder", CODE_FOLDERS)
def test_every_write_entry_point_refuses_a_code_folder(folder: str) -> None:
    target = REPO_ROOT / folder / "nested" / "runtime.json"
    with pytest.raises(RuntimeWriteRefused):
        ensure_runtime_path(target)
    with pytest.raises(RuntimeWriteRefused):
        make_dirs(target.parent)
    with pytest.raises(RuntimeWriteRefused):
        open_runtime(target, "ab")
    with pytest.raises(RuntimeWriteRefused):
        write_bytes(target, b"x")
    with pytest.raises(RuntimeWriteRefused):
        write_text(target, "x")
    assert not (REPO_ROOT / folder / "nested").exists()


def test_a_dotted_path_back_into_a_code_folder_is_refused() -> None:
    with pytest.raises(RuntimeWriteRefused):
        ensure_runtime_path(REPO_ROOT / "config" / ".." / "src" / "x.json")


def test_gitignored_state_folders_stay_allowed() -> None:
    for folder in ("config", "webapp", "logs"):
        assert ensure_runtime_path(REPO_ROOT / folder / "x.json") == (REPO_ROOT / folder / "x.json").resolve()


def test_paths_outside_the_checkout_are_written(tmp_path: Path) -> None:
    make_dirs(tmp_path / "a" / "b")
    write_text(tmp_path / "a" / "b" / "t.txt", "héllo")
    write_bytes(tmp_path / "a" / "b" / "b.bin", b"\x00\x01")
    with open_runtime(tmp_path / "a" / "b" / "t.txt", "a", encoding="utf-8") as fh:
        fh.write("!")
    assert (tmp_path / "a" / "b" / "t.txt").read_text(encoding="utf-8") == "héllo!"
    assert (tmp_path / "a" / "b" / "b.bin").read_bytes() == b"\x00\x01"


def test_a_refusal_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger=runtime_files.logger.name):
        with pytest.raises(RuntimeWriteRefused):
            ensure_runtime_path(REPO_ROOT / "src" / "x.json")
    assert "refused a runtime write under src/" in caplog.text
