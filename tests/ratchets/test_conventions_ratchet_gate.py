"""The gate's conventions-ratchet stage maps the vendored script's exit codes.

`scripts/conventions_ratchet.py` exits 3 when git is unusable or a tracked Python file
does not parse: nothing was checked. These tests pin that 3 fails as NOT CONFIRMED --
never a pass, never a skip -- using the real script's exit 3, that a new
print in library code turns the gate red by rule and reverting it turns it green, and that the
pre-ship gate calls the wrapper that guarantees it. They run in throwaway git repos:
no network, no device or cloud API.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from scripts import conventions_ratchet_gate as gate
from tests import _ratchet_gate_helpers as helpers

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "conventions_ratchet.py"
_exit = partial(helpers.real_exit, REAL_SCRIPT)

_FLEET = (
    "[conventions_ratchet]\n"
    "enabled = true\n"
    'baseline = ".conventions-ratchet-baseline.json"\n'
    'rules = ["logging-not-print", "no-drive-path"]\n'
)
_TREE = {"src/mod.py": "import logging\n\nlog = logging.getLogger(__name__)\n\n\ndef used() -> int:\n    log.info('x')\n    return 1\n"}
_PRINTS = _TREE["src/mod.py"] + "\n\ndef noisy() -> None:\n    print('x')\n"


def _repo(tmp_path: Path) -> Path:
    return helpers.make_git_repo(tmp_path, _FLEET, _TREE)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


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
    proc = helpers.real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "src/mod.py" in proc.stdout and "[logging-not-print]" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / "src" / "mod.py").write_text(_TREE["src/mod.py"], encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "conventions ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper("conventions_ratchet", "conventions ratchet", "dead code ratchet")


def test_the_ratchet_is_declared_and_its_script_is_vendored() -> None:
    helpers.assert_ratchet_declared_and_vendored("conventions_ratchet")
