"""The e2e harness's ``_adopt_port()`` must fail loud, never guess (#593).

fleet-config#537 fixed the common case (a worktree's copied config is now
rewritten to a non-colliding port), but the pre-existing bare ``except
Exception: return 8447`` fallback still silently guessed the primary's port
whenever the config genuinely couldn't be resolved. From inside a worktree
that guess is wrong -- it declares a false collision with the primary's live
tray, the exact false positive this function exists to avoid. The fix
removes the guess: a config that fails to parse cleanly now propagates
instead of being papered over. (A missing config is simply "use defaults",
pinned by ``tests/test_webapp_config.py``.)

A plain unit test rather than a browser test: nothing here touches a page,
and under ``tests/e2e`` the autouse browser fixtures ran it once per
projection (#732).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import webapp_config
from tests.e2e.conftest import _adopt_port


def test_adopt_port_raises_on_invalid_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = tmp_path / "webapp_config.json"
    bad.write_text('{"port": 99999}', encoding="utf-8")  # out of range
    monkeypatch.setattr(webapp_config, "DEFAULT_CONFIG_PATH", bad)

    with pytest.raises(ValueError):
        _adopt_port()
