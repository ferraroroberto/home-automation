"""The e2e autoboot must keep every runtime database off the real folder (#975).

The harness used to redirect only the telemetry and usage databases, file by
file; the energy-history, network-history and network-survey databases
resolved through ``runtime_db_path`` stayed on the owner's real runtime
folder (project-scaffolding#404). The fix points the app-level
``HOME_AUTOMATION_DATA_DIR`` at a per-run temp folder and drops the inherited
per-file overrides that would outrank it.

A plain unit test rather than a browser test: nothing here touches a page.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src._no_window import NO_WINDOW
from src.runtime_data import app_dir_env_var
from tests.e2e.conftest import _REPO_ROOT, _autoboot_env

_DATA_DIR_VAR = app_dir_env_var("home-automation")

_RESOLVE_SNIPPET = """
import json
from src import energy_history, network_history, network_survey, telemetry
print(json.dumps({
    m.__name__: str(m.DEFAULT_DB_PATH)
    for m in (energy_history, network_history, network_survey, telemetry)
}))
"""


def test_autoboot_env_sets_the_app_data_dir(tmp_path: Path) -> None:
    env = _autoboot_env(tmp_path)

    assert env[_DATA_DIR_VAR] == str(tmp_path)


def test_autoboot_env_drops_inherited_per_file_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("ENERGY_HISTORY_DB_PATH", "NETWORK_HISTORY_DB_PATH", "NETWORK_SURVEY_DB_PATH"):
        monkeypatch.setenv(name, str(tmp_path / "elsewhere" / name))

    env = _autoboot_env(tmp_path / "run")

    for name in ("ENERGY_HISTORY_DB_PATH", "NETWORK_HISTORY_DB_PATH", "NETWORK_SURVEY_DB_PATH"):
        assert name not in env


def test_autoboot_env_resolves_every_runtime_db_under_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolve the databases in a child process under the harness's env, with
    an inherited per-file override trying to outrank the directory."""
    monkeypatch.setenv("NETWORK_HISTORY_DB_PATH", str(tmp_path / "real" / "nh.sqlite3"))
    data_dir = tmp_path / "run"
    env = _autoboot_env(data_dir)

    out = subprocess.run(
        [sys.executable, "-c", _RESOLVE_SNIPPET],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, check=True,
        creationflags=NO_WINDOW,
    ).stdout
    resolved = json.loads(out)

    for module, path in resolved.items():
        parent = Path(path).parent
        if module == "src.telemetry":
            # Telemetry keeps its throwaway per-file override (#296).
            continue
        assert parent == data_dir, f"{module} resolved to {path}, outside {data_dir}"
