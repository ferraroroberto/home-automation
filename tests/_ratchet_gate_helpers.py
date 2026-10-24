"""Shared plumbing for the "run the real vendored ratchet script" gate tests.

Every ratchet gate test exercises its script against a throwaway git repo,
maps its wrapper's exit codes and checks its gate stage and `.fleet.toml`
declaration identically — only the script path, the stage label and the key
differ — so that shape lives here once.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

from src._no_window import NO_WINDOW

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_git_repo(tmp_path: Path, fleet_toml: str, files: dict[str, str]) -> Path:
    """Commit `files` (plus `fleet_toml` as `.fleet.toml`) into a fresh repo at `tmp_path`."""
    (tmp_path / ".fleet.toml").write_text(fleet_toml, encoding="utf-8")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    # An empty hooks dir keeps the machine's global commit hooks out of the throwaway repo.
    no_hooks = tmp_path.parent / f"{tmp_path.name}-no-hooks"
    no_hooks.mkdir()
    for argv in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c", f"core.hooksPath={no_hooks}", "-c", "user.name=t",
            "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x",
        ],
    ):
        subprocess.run(
            ["git", *argv], cwd=tmp_path, check=True, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    return tmp_path


def real_run(script: Path, repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the real vendored `script` against `repo`, capturing stdout/stderr."""
    return subprocess.run(
        [sys.executable, str(script), "--root", str(repo), *flags],
        cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        creationflags=NO_WINDOW,
    )


def real_exit(script: Path, repo: Path, *flags: str) -> int:
    """Run the real vendored `script` against `repo` and return its exit code."""
    return real_run(script, repo, *flags).returncode


def assert_run_maps_every_script_exit(
    gate: ModuleType, stage: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`gate.run` echoes a fake script's output and prints `stage`'s OK/FAIL verdict
    for every exit code it can return (0 passes, everything else fails)."""
    for code in (0, 1, 2, 3):
        fake = tmp_path / "fake.py"
        fake.write_text(f"import sys\nprint('from the script')\nsys.exit({code})\n", encoding="utf-8")
        assert gate.run(fake) == (0 if code == 0 else 1), code
        out = capsys.readouterr().out
        assert "from the script" in out
        assert out.rstrip().splitlines()[-1].startswith(
            f"OK: {stage}" if code == 0 else f"FAIL: {stage}"
        )


def assert_only_exit_zero_passes_and_three_is_not_confirmed(gate: ModuleType) -> None:
    """`gate.classify_exit` passes on 0 alone, and labels 3 NOT CONFIRMED, never a skip."""
    assert gate.classify_exit(0)[0] is True
    for code in (1, 2, 3, 4, -1):
        assert gate.classify_exit(code)[0] is False, code
    ok, label = gate.classify_exit(3)
    assert not ok
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


def assert_gate_stage_calls_the_wrapper(
    key: str, stage: str, previous_stage: Optional[str] = None
) -> None:
    """The pre-ship gate runs `key` through its `-m scripts.<key>_gate` wrapper, never the
    bare script, and (when given) right after `previous_stage`."""
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stages = [line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")]
    own = [line for line in stages if key in line]
    assert len(own) == 1
    assert f"-m scripts.{key}_gate" in own[0]
    assert f"{key}.py" not in own[0]
    if previous_stage is not None:
        names = [line.split('"')[1] for line in stages]
        assert names.index(stage) == names.index(previous_stage) + 1


def assert_ratchet_declared_and_vendored(key: str) -> dict:
    """`.fleet.toml` enables ratchet `key`, its baseline exists and its script is vendored.
    Returns the parsed `.fleet.toml` for a caller's extra assertions."""
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet[key]["enabled"] is True
    assert (REPO_ROOT / fleet[key]["baseline"]).is_file()
    entry = fleet["vendored"][key]
    assert entry["dest"] == f"scripts/{key}.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
    return fleet
