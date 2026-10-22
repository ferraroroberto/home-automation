"""Shared plumbing for the "run the real vendored ratchet script" gate tests.

Both `test_docs_ratchet_gate.py` and `test_comment_ratchet_gate.py` exercise
their script against a throwaway git repo and their wrapper's exit-code
mapping identically — only the script path and the stage label differ — so
that shape lives here once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


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
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, stdin=subprocess.DEVNULL)
    return tmp_path


def real_run(script: Path, repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the real vendored `script` against `repo`, capturing stdout/stderr."""
    return subprocess.run(
        [sys.executable, str(script), "--root", str(repo), *flags],
        cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
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
