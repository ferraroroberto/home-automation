"""The gate's dead-code-ratchet stage maps the vendored script's exit codes.

`scripts/dead_code_ratchet.py` exits 3 when git is unusable or a tracked Python file
does not parse: nothing was checked. These tests pin that 3 fails as NOT CONFIRMED --
never a pass, never a skip -- using the real script's exit 3, that a new uncalled
function turns the gate red by name and removing it turns it green, and that the
pre-ship gate calls the wrapper that guarantees it. They run in throwaway git repos:
no network, no device or cloud API.
"""

from __future__ import annotations

import tomllib
from functools import partial
from pathlib import Path

import pytest

from scripts import dead_code_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_only_exit_zero_passes_and_three_is_not_confirmed,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "dead_code_ratchet.py"
_exit = partial(real_exit, REAL_SCRIPT)

_FLEET = '[dead_code_ratchet]\nenabled = true\nbaseline = ".dead-code-ratchet-baseline.json"\n'
_TREE = {"pkg/mod.py": "def used():\n    return 1\n\n\nprint(used())\n"}
_DEAD = _TREE["pkg/mod.py"] + "\n\ndef orphan_helper():\n    return 2\n"


def _repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path, _FLEET, _TREE)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "pkg" / "mod.py").write_text("def broken(:\n", encoding="utf-8")
    code = _exit(repo)
    assert code == 3  # an unparseable file may hold the only reference to a definition: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_an_uncalled_function_turns_the_gate_red_by_name_and_reverting_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / "pkg" / "mod.py").write_text(_DEAD, encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "pkg/mod.py" in proc.stdout and "orphan_helper" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / "pkg" / "mod.py").write_text(_TREE["pkg/mod.py"], encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "dead code ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")]
    dead_code = [line for line in stages if "dead_code_ratchet" in line]
    assert len(dead_code) == 1
    assert "-m scripts.dead_code_ratchet_gate" in dead_code[0]
    assert "dead_code_ratchet.py" not in dead_code[0]
    names = [line.split('"')[1] for line in stages]
    assert names.index("dead code ratchet") == names.index("doc refs ratchet") + 1


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["dead_code_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["dead_code_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["dead_code_ratchet"]
    assert entry["dest"] == "scripts/dead_code_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
