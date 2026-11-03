"""The gate's usage-track-ratchet stage maps the vendored script's exit codes.

`scripts/usage_track_ratchet.py` exits 3 when a file cannot be read or its JS never
closes: nothing was checked. These tests pin that 3 fails as NOT CONFIRMED, that a new
untracked control turns the gate red and removing it turns it green, and that the
pre-ship gate calls the wrapper that guarantees it. They run in throwaway git repos.
"""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from scripts import usage_track_ratchet_gate as gate
from src._no_window import NO_WINDOW
from tests import _ratchet_gate_helpers as helpers

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "usage_track_ratchet.py"
_exit = partial(helpers.real_exit, REAL_SCRIPT)

_FLEET = (
    "[usage_track_ratchet]\n"
    "enabled = true\n"
    'baseline = ".usage-track-ratchet-baseline.json"\n'
    'rules = ["untracked-control"]\n'
)
_PAGE = "app/webapp/static/index.html"
_TRACKED = '<button data-track="save">Save</button>\n'
_UNTRACKED = _TRACKED + "<button>Delete</button>\n"


def _repo(tmp_path: Path) -> Path:
    return helpers.make_git_repo(tmp_path, _FLEET, {_PAGE: _TRACKED})


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "app" / "webapp" / "static" / "app.js").write_text("const s = 'never closed;\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, creationflags=NO_WINDOW)
    code = _exit(repo)
    assert code == 3  # JS that never closes leaves every later token a guess: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_a_new_untracked_control_turns_the_gate_red_and_removing_it_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / _PAGE).write_text(_UNTRACKED, encoding="utf-8")
    proc = helpers.real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert _PAGE in proc.stdout and "[untracked-control]" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / _PAGE).write_text(_TRACKED, encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "usage track ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper(
        "usage_track_ratchet", "usage track ratchet", "runtime write ratchet"
    )


def test_the_ratchet_and_its_reader_are_declared_and_vendored() -> None:
    fleet = helpers.assert_ratchet_declared_and_vendored("usage_track_ratchet")
    reader = fleet["vendored"]["usage_track_scan"]
    assert reader["dest"] == "scripts/usage_track_scan.py"
    assert (helpers.REPO_ROOT / reader["dest"]).is_file()
