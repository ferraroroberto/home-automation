"""The gate's history-marker-ratchet stage maps the script's exit codes.

`scripts/history_marker_ratchet.py` exits 3 when git is unusable or a file
could not be read: nothing was checked. These tests pin that 3 fails as NOT
CONFIRMED -- never a pass, never a skip -- using the real script's exit 3,
that a genuine history-narration phrase turns the gate red, and that the
pre-ship gate calls the wrapper that guarantees it. They run in throwaway git
repos: no network, no device or cloud API.

Unlike the vendored ratchets, this script is repo-specific (audit #1044's
structural check), so there is no `[vendored]` declaration to pin here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts import history_marker_ratchet_gate as gate
from tests._ratchet_gate_helpers import (
    assert_gate_stage_calls_the_wrapper,
    assert_only_exit_zero_passes_and_three_is_not_confirmed,
    assert_run_maps_every_script_exit,
    make_git_repo,
    real_exit,
    real_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "history_marker_ratchet.py"

_FLEET = '[history_marker_ratchet]\nenabled = true\nbaseline = ".history-marker-ratchet-baseline.json"\n'


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway repo that opts in to the history marker ratchet, with `files` committed."""
    return make_git_repo(tmp_path, _FLEET, files)


def _real_exit(repo: Path, *flags: str) -> int:
    return real_exit(REAL_SCRIPT, repo, *flags)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"a.py": "x = 1  # plain comment\n"})
    assert _real_exit(repo, "--init") == 0
    (repo / "a.py").write_bytes(b"\xff\xfe not valid utf-8\x00")
    code = _real_exit(repo)
    assert code == 3  # not confirmed, as the script documents
    assert gate.classify_exit(code)[0] is False


def test_a_history_narration_phrase_turns_the_gate_red_by_name(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, {"a.py": '"""Plain docstring."""\nx = 1\n'})
    assert _real_exit(repo, "--init") == 0
    assert _real_exit(repo) == 0
    (repo / "a.py").write_text(
        '"""This used to be a private helper, extracted verbatim from elsewhere."""\nx = 1\n',
        encoding="utf-8",
    )
    proc = real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "a.py" in proc.stdout and "used to be" in proc.stdout.lower()
    assert gate.classify_exit(proc.returncode)[0] is False


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_run_maps_every_script_exit(gate, "history marker ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    assert_gate_stage_calls_the_wrapper(
        "history_marker_ratchet", "history marker ratchet", "conventions ratchet"
    )


def test_the_ratchet_is_declared_and_its_baseline_exists() -> None:
    """Repo-specific, not vendored: enabled + baseline only, no `[vendored]` entry."""
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    cfg = fleet["history_marker_ratchet"]
    assert cfg["enabled"] is True
    assert (REPO_ROOT / cfg["baseline"]).is_file()
    assert "history_marker_ratchet" not in fleet.get("vendored", {})
