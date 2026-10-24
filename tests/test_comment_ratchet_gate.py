"""The gate's comment-ratchet stage maps the vendored script's exit codes.

`scripts/comment_ratchet.py` exits 3 when git is unusable or a file could not
be scanned (unterminated string/comment, bytes that are not UTF-8): nothing
was checked. These tests pin that 3 fails as NOT CONFIRMED -- never a pass,
never a skip -- using the real script's exit 3, and that the pre-ship gate
calls the wrapper that guarantees it. They run in throwaway git repos: no
network, no device or cloud API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import comment_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_gate_stage_calls_the_wrapper,
    assert_only_exit_zero_passes_and_three_is_not_confirmed,
    assert_ratchet_declared_and_vendored,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "comment_ratchet.py"

_FLEET = '[comment_ratchet]\nenabled = true\nbaseline = ".comment-ratchet-baseline.json"\n'


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway repo that opts in to the comment ratchet, with `files` committed."""
    return make_git_repo(tmp_path, _FLEET, files)


def _real_exit(repo: Path, *flags: str) -> int:
    return real_exit(REAL_SCRIPT, repo, *flags)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"a.py": "x = 1  # plain comment\n"})
    assert _real_exit(repo, "--init") == 0
    (repo / "a.py").write_text("s = '''never closed\n", encoding="utf-8")
    code = _real_exit(repo)
    assert code == 3  # not confirmed, as the script documents
    assert gate.classify_exit(code)[0] is False


def test_a_new_issue_reference_turns_the_gate_red_by_name(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"a.py": "x = 1  # plain comment\n"})
    assert _real_exit(repo, "--init") == 0
    assert _real_exit(repo) == 0
    (repo / "a.py").write_text("x = 1  # fixed in #123\n", encoding="utf-8")
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "a.py" in proc.stdout and "#123" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "comment ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    assert_gate_stage_calls_the_wrapper("comment_ratchet", "comment ratchet", "docs ratchet")


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    assert_ratchet_declared_and_vendored("comment_ratchet")
