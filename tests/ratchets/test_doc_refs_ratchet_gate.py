"""The gate's doc-refs-ratchet stage maps the vendored script's exit codes.

`scripts/doc_refs_ratchet.py` exits 3 when git is unusable, a doc is not UTF-8 or a
fenced block never closes: nothing was checked. These tests pin that 3 fails as NOT
CONFIRMED -- never a pass, never a skip -- using the real script's exit 3, that a doc
naming a missing path turns the gate red by name and reverting turns it green, and that
the pre-ship gate calls the wrapper that guarantees it. They run in throwaway git
repos: no network, no device or cloud API.
"""

from __future__ import annotations

import tomllib
from functools import partial
from pathlib import Path

import pytest

from scripts import doc_refs_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_only_exit_zero_passes_and_three_is_not_confirmed,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "doc_refs_ratchet.py"
_exit = partial(real_exit, REAL_SCRIPT)

_FLEET = '[doc_refs_ratchet]\nenabled = true\nbaseline = ".doc-refs-ratchet-baseline.json"\n'
_TREE = {"scripts/real.py": "x = 1\n", "docs/guide.md": "Run `scripts/real.py`.\n"}
_STALE = "Run `scripts/real.py` or `scripts/gone.py`.\n"


def _repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path, _FLEET, _TREE)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "docs" / "guide.md").write_text("```\nnever closed\n", encoding="utf-8")
    code = _exit(repo)
    assert code == 3  # an unclosed fence would hide every reference below it: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_a_reference_to_a_missing_path_turns_the_gate_red_by_name_and_reverting_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / "docs" / "guide.md").write_text(_STALE, encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "docs/guide.md" in proc.stdout and "scripts/gone.py" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / "docs" / "guide.md").write_text(_TREE["docs/guide.md"], encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "doc refs ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")]
    doc_refs = [line for line in stages if "doc_refs_ratchet" in line]
    assert len(doc_refs) == 1
    assert "-m scripts.doc_refs_ratchet_gate" in doc_refs[0]
    assert "doc_refs_ratchet.py" not in doc_refs[0]
    names = [line.split('"')[1] for line in stages]
    assert names.index("doc refs ratchet") == names.index("instruction ratchet") + 1


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["doc_refs_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["doc_refs_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["doc_refs_ratchet"]
    assert entry["dest"] == "scripts/doc_refs_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
