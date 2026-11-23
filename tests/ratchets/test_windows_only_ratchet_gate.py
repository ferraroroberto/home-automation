"""The gate's windows-only-ratchet stage maps the vendored script's exit codes.

`scripts/windows_only_ratchet.py` exits 3 when git cannot list the tree or a test module cannot
be read or parsed: nothing was checked. These tests pin that 3 fails as NOT CONFIRMED, that an
unmarked Windows-only test turns the gate red and marking it turns it green, and that the
pre-ship gate calls the wrapper that guarantees it. They run in throwaway git repos.
"""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from scripts import windows_only_ratchet_gate as gate
from src._no_window import NO_WINDOW
from tests import _ratchet_gate_helpers as helpers

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "windows_only_ratchet.py"
_exit = partial(helpers.real_exit, REAL_SCRIPT)

_FLEET = (
    "[windows_only_ratchet]\n"
    "enabled = true\n"
    'baseline = ".windows-only-ratchet-baseline.json"\n'
)
_TEST = "tests/test_spawn.py"
_CLEAN = "def test_spawn():\n    assert True\n"
_UNMARKED = (
    "import subprocess\n\n\n"
    "def test_spawn():\n    assert subprocess.CREATE_NO_WINDOW\n"
)
_MARKED = (
    "import subprocess\n\nimport pytest\n\n\n"
    "@pytest.mark.windows_only\n"
    "def test_spawn():\n    assert subprocess.CREATE_NO_WINDOW\n"
)


def _repo(tmp_path: Path) -> Path:
    return helpers.make_git_repo(tmp_path, _FLEET, {_TEST: _CLEAN})


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, creationflags=NO_WINDOW)
    code = _exit(repo)
    assert code == 3  # a module that does not parse cannot be judged: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_an_unmarked_windows_only_test_turns_the_gate_red_and_marking_it_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / _TEST).write_text(_UNMARKED, encoding="utf-8")
    proc = helpers.real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert _TEST in proc.stdout and "win-api" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / _TEST).write_text(_MARKED, encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "windows only ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper(
        "windows_only_ratchet", "windows only ratchet", "request budget ratchet"
    )


def test_the_ratchet_is_declared_and_vendored() -> None:
    helpers.assert_ratchet_declared_and_vendored("windows_only_ratchet")
