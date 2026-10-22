"""The gate's docs-ratchet stage maps the vendored script's exit codes.

`scripts/docs_ratchet.py` exits 3 when git or a doc could not be read, or a
fenced block never closes: nothing was checked. These tests pin that 3 fails as
NOT CONFIRMED -- never a pass, never a skip -- using the real script's exit 3,
and that the pre-ship gate calls the wrapper that guarantees it. They run in
throwaway git repos: no network, no device or cloud API.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import docs_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "docs_ratchet.py"

_FLEET = '[docs_ratchet]\nenabled = true\nbaseline = ".docs-ratchet-baseline.json"\n'


def _git_repo(tmp_path: Path, readme: str) -> Path:
    """A throwaway repo that opts in to the docs ratchet, with `readme` committed."""
    return make_git_repo(tmp_path, _FLEET, {"README.md": readme})


def _real_exit(repo: Path, *flags: str) -> int:
    return real_exit(REAL_SCRIPT, repo, *flags)


def test_only_exit_zero_passes() -> None:
    assert gate.classify_exit(0)[0] is True
    for code in (1, 2, 3, 4, -1):
        assert gate.classify_exit(code)[0] is False, code


def test_exit_three_is_not_confirmed_and_never_a_skip() -> None:
    ok, label = gate.classify_exit(3)
    assert not ok
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "# Title\n\nPlain prose.\n")
    assert _real_exit(repo, "--init") == 0
    (repo / "README.md").write_text("# Title\n\n```\nnever closed\n", encoding="utf-8")
    code = _real_exit(repo)
    assert code == 3  # not confirmed, as the script documents
    assert gate.classify_exit(code)[0] is False


def test_a_new_issue_reference_turns_the_gate_red_by_name(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "# Title\n\nPlain prose.\n")
    assert _real_exit(repo, "--init") == 0
    assert _real_exit(repo) == 0
    (repo / "README.md").write_text("# Title\n\nAdded in #123.\n", encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "README.md" in proc.stdout and "#123" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "docs ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stage_lines = [
        line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")
    ]
    docs = [line for line in stage_lines if "docs_ratchet" in line]
    assert len(docs) == 1
    assert "-m scripts.docs_ratchet_gate" in docs[0]
    assert "docs_ratchet.py" not in docs[0]
    names = [line.split('"')[1] for line in stage_lines]
    assert names.index("docs ratchet") == names.index("static ratchet") + 1


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["docs_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["docs_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["docs_ratchet"]
    assert entry["dest"] == "scripts/docs_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
