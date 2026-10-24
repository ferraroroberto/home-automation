"""The gate's structure-ratchet stage maps the vendored script's exit codes.

`scripts/structure_ratchet.py` exits 3 when git is unusable, a file cannot be
read or parsed, or `shape` is not a known preset: nothing was checked. These
tests pin that 3 fails as NOT CONFIRMED -- never a pass, never a skip -- using
the real script's exit 3, that growing a baselined file turns the gate red by
name and reverting turns it green, and that the pre-ship gate calls the wrapper
that guarantees it. They run in throwaway git repos: no network, no device or
cloud API.
"""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from scripts import structure_ratchet_gate as gate
from src._no_window import NO_WINDOW
from tests._ratchet_gate_helpers import (
    assert_gate_stage_calls_the_wrapper,
    assert_ratchet_declared_and_vendored,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "structure_ratchet.py"
_exit = partial(real_exit, REAL_SCRIPT)

# max_file_lines = 5 makes a small fixture an offender without a 1,000-line file.
_FLEET = (
    'shape = "webapp-tray"\n'
    '[structure_ratchet]\nenabled = true\nbaseline = ".structure-ratchet-baseline.json"\n'
    "max_file_lines = 5\n"
)
_TREE = {
    "app/__init__.py": "",
    "src/a.py": "x = 1\n",
    "tests/test_a.py": "y = 1\n",
    "docs/readme.md": "docs\n",
}
_OVER_BUDGET = "".join(f"v{i} = {i}\n" for i in range(8))


@pytest.mark.parametrize("code", [1, 2, 3, 4, -1])
def test_every_exit_but_zero_fails(code: int) -> None:
    assert gate.classify_exit(0)[0] is True
    assert gate.classify_exit(code)[0] is False


def test_exit_three_is_not_confirmed_and_never_a_skip() -> None:
    ok, label = gate.classify_exit(3)
    assert not ok
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, _FLEET, _TREE)
    assert _exit(repo, "--init") == 0
    (repo / "src" / "a.py").write_text("def broken(:\n", encoding="utf-8")
    code = _exit(repo)
    assert code == 3  # a Python file that does not parse: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_growing_a_baselined_file_turns_the_gate_red_and_reverting_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path, _FLEET, {**_TREE, "src/big.py": _OVER_BUDGET})
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / "src" / "big.py").write_text(_OVER_BUDGET + "extra = 1\n", encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "src/big.py" in proc.stdout
    (repo / "src" / "big.py").write_text(_OVER_BUDGET, encoding="utf-8")
    assert _exit(repo) == 0


def test_an_undeclared_root_folder_turns_the_gate_red_by_name(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, _FLEET, _TREE)
    assert _exit(repo, "--init") == 0
    (repo / "stray").mkdir()
    (repo / "stray" / "x.py").write_text("z = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "stray/x.py"], cwd=repo, check=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "stray" in proc.stdout


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "structure ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    assert_gate_stage_calls_the_wrapper("structure_ratchet", "structure ratchet", "comment ratchet")


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = assert_ratchet_declared_and_vendored("structure_ratchet")
    assert fleet["shape"] == "webapp-tray"
    assert set(fleet["extra_roots"]) == {"custom_components", "spike"}
