"""The gate's conventions-ratchet stage maps the vendored script's exit codes.

`scripts/conventions_ratchet.py` exits 3 when git is unusable or a tracked Python file
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

from scripts import conventions_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_only_exit_zero_passes_and_three_is_not_confirmed,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "conventions_ratchet.py"
_exit = partial(real_exit, REAL_SCRIPT)

_FLEET = (
    "[conventions_ratchet]\n"
    "enabled = true\n"
    'baseline = ".conventions-ratchet-baseline.json"\n'
    'rules = ["logging-not-print", "no-drive-path"]\n'
)
_TREE = {"src/mod.py": "import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef used() -> int:\n    log.info('x')\n    return 1\n"}
_PRINTS = _TREE["src/mod.py"] + "\n\ndef noisy() -> None:\n    print('x')\n"


def _repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path, _FLEET, _TREE)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "src" / "mod.py").write_text("def broken(:\n", encoding="utf-8")
    code = _exit(repo)
    assert code == 3  # an unparseable file may hold the only violation: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_a_new_print_in_library_code_turns_the_gate_red_by_rule_and_reverting_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / "src" / "mod.py").write_text(_PRINTS, encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "src/mod.py" in proc.stdout and "[logging-not-print]" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / "src" / "mod.py").write_text(_TREE["src/mod.py"], encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "conventions ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")]
    conventions = [line for line in stages if "conventions_ratchet" in line]
    assert len(conventions) == 1
    assert "-m scripts.conventions_ratchet_gate" in conventions[0]
    assert "conventions_ratchet.py" not in conventions[0]
    names = [line.split('"')[1] for line in stages]
    assert names.index("conventions ratchet") == names.index("dead code ratchet") + 1


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["conventions_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["conventions_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["conventions_ratchet"]
    assert entry["dest"] == "scripts/conventions_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
