"""The gate's ux-surface-ratchet stage maps the vendored script's exit codes.

`scripts/ux_surface_ratchet.py` exits 3 when a file cannot be read or its JS never closes:
nothing was checked. These tests pin that 3 fails as NOT CONFIRMED, that a surface root with
no `data-surface` turns the gate red and marking it turns it green, and that the pre-ship
gate calls the wrapper that guarantees it. They run in throwaway git repos.
"""

from __future__ import annotations

import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

from scripts import ux_surface_ratchet_gate as gate
from src._no_window import NO_WINDOW
from tests import _ratchet_gate_helpers as helpers

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPT = REPO_ROOT / "scripts" / "ux_surface_ratchet.py"
_exit = partial(helpers.real_exit, REAL_SCRIPT)

_FLEET = (
    "[ux_surface_ratchet]\n"
    "enabled = true\n"
    'baseline = ".ratchets/ux-surface.json"\n'
    'map = "docs/ux-surfaces.yaml"\n'
    'paths = ["app/webapp/static/**"]\n'
)
_PAGE = "app/webapp/static/index.html"
_MARKED = '<dialog data-surface="demo.sheet"></dialog>\n'
_UNMARKED = '<dialog data-surface="demo.sheet"></dialog>\n<dialog id="other"></dialog>\n'
_MAP = (
    "app: demo\n"
    "walk:\n"
    "  date: 2026-01-01\n"
    "  build: abc1234\n"
    "  widths: [390]\n"
    "  themes: [light]\n"
    "areas:\n"
    "  - id: demo\n"
    "    title: Demo\n"
    "surfaces:\n"
    "  - id: demo.sheet\n"
    "    area: demo\n"
    "    title: Sheet\n"
    "    root: data-surface\n"
)


def _git_add(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, creationflags=NO_WINDOW)


def _repo(tmp_path: Path) -> Path:
    repo = helpers.make_git_repo(tmp_path, _FLEET, {_PAGE: _MARKED, "docs/ux-surfaces.yaml": _MAP})
    (repo / "scripts").mkdir(exist_ok=True)
    for name in ("ux_surface_ratchet.py", "ux_surface_map.py", "usage_track_scan.py", "ratchet_baseline.py"):
        (repo / "scripts" / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
    generated = subprocess.run(
        [sys.executable, str(repo / "scripts" / "ux_surface_map.py")],
        cwd=repo, capture_output=True, check=False, creationflags=NO_WINDOW, timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    _git_add(repo)
    return repo


def _script_exit(repo: Path, *flags: str) -> int:
    return helpers.real_exit(repo / "scripts" / "ux_surface_ratchet.py", repo, *flags)


def test_only_exit_zero_passes_and_exit_three_is_not_confirmed() -> None:
    helpers.assert_only_exit_zero_passes_and_three_is_not_confirmed(gate)


def test_the_real_scripts_exit_three_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _script_exit(repo, "--init") == 0
    (repo / "app" / "webapp" / "static" / "app.js").write_text("const s = 'never closed;\n", encoding="utf-8")
    _git_add(repo)
    code = _script_exit(repo)
    assert code == 3  # JS that never closes leaves every later token a guess: not confirmed
    assert gate.classify_exit(code)[0] is False


def test_an_unmarked_surface_turns_the_gate_red_and_marking_it_turns_it_green(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _script_exit(repo, "--init") == 0
    assert _script_exit(repo) == 0
    (repo / _PAGE).write_text(_UNMARKED, encoding="utf-8")
    proc = helpers.real_run(repo / "scripts" / "ux_surface_ratchet.py", repo)
    assert proc.returncode == 1
    assert _PAGE in proc.stdout and "[unmarked-surface]" in proc.stdout
    assert gate.classify_exit(proc.returncode)[0] is False
    (repo / _PAGE).write_text(_MARKED, encoding="utf-8")
    assert _script_exit(repo) == 0


def test_run_exits_nonzero_for_every_script_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    helpers.assert_run_maps_every_script_exit(gate, "ux surface ratchet", tmp_path, capsys)


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script() -> None:
    helpers.assert_gate_stage_calls_the_wrapper(
        "ux_surface_ratchet", "ux surface ratchet", "usage track ratchet"
    )


def test_the_ratchet_and_its_readers_are_declared_and_vendored() -> None:
    fleet = helpers.assert_ratchet_declared_and_vendored("ux_surface_ratchet")
    for key in ("usage_track_scan", "ux_surface_map"):
        reader = fleet["vendored"][key]
        assert reader["dest"] == f"scripts/{key}.py"
        assert (helpers.REPO_ROOT / reader["dest"]).is_file()
