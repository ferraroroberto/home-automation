"""The gate's probe-fake-ratchet stage maps the vendored script's exit codes.

`scripts/probe_fake_ratchet.py` exits 3 when git cannot list the tree or find the merge base, or
a file cannot be read or parsed: nothing was checked. These tests pin that 3 fails as NOT
CONFIRMED, that a probe change with no test change turns the gate red and moving its test with it
turns it green, and that the pre-ship gate calls the wrapper that guarantees it. They run in
throwaway git repos.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from scripts import probe_fake_ratchet_gate as gate
from tests import _ratchet_gate_helpers as helpers

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "probe_fake_ratchet.py"
_exit = partial(helpers.real_exit, REAL_SCRIPT)

# `base_ref = "HEAD"`: the throwaway repo has no remote, so the diff is the uncommitted work.
_FLEET = (
    "[probe_fake_ratchet]\n"
    "enabled = true\n"
    'baseline = ".ratchets/probe-fake.json"\n'
    'probes = ["src/ports.py"]\n'
    'base_ref = "HEAD"\n'
)
_PROBE = "src/ports.py"
_TEST = "tests/test_ports.py"
_FAKES = (
    "from src import ports\n\n\n"
    "def test_holders_win32():\n    assert ports\n\n\n"
    "def test_holders_darwin():\n    assert ports\n"
)


def _repo(tmp_path: Path) -> Path:
    return helpers.make_git_repo(tmp_path, _FLEET, {_PROBE: "PORT = 1\n", _TEST: _FAKES})


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    (repo / "tests" / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    code = _exit(repo)
    assert code == 3  # a module that does not parse cannot be judged: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_a_probe_change_without_its_test_turns_the_gate_red_and_moving_the_test_turns_it_green(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _exit(repo, "--init") == 0
    assert _exit(repo) == 0
    (repo / _PROBE).write_text("PORT = 2\n", encoding="utf-8")
    proc = helpers.real_run(REAL_SCRIPT, repo)
    assert proc.returncode == 1
    assert "probe-changed-no-fake" in proc.stdout and "MAC_NIGHTLY: unverified" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / _TEST).write_text(_FAKES + "\n# moved with the probe\n", encoding="utf-8")
    assert _exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "probe fake ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper(
        "probe_fake_ratchet", "probe fake ratchet", "windows only ratchet"
    )


def test_the_ratchet_is_declared_and_vendored() -> None:
    fleet = helpers.assert_ratchet_declared_and_vendored("probe_fake_ratchet")
    probes = fleet["probe_fake_ratchet"]["probes"]
    assert probes and all((REPO_ROOT / probe).is_file() for probe in probes)
