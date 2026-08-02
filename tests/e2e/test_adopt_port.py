"""``_adopt_port()`` must fail loud, never guess, on a broken config (#593).

fleet-config#537 fixed the common case (a worktree's copied config is now
rewritten to a non-colliding port), but the pre-existing bare ``except
Exception: return 8447`` fallback still silently guessed the primary's port
whenever the config genuinely couldn't be resolved. From inside a worktree
that guess is wrong -- it declares a false collision with the primary's live
tray, the exact false positive this function exists to avoid. The fix
removes the guess: a config that fails to parse cleanly (as opposed to one
that's simply missing, which ``load_webapp_config()`` already treats as "use
defaults") now propagates instead of being papered over.
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


def test_adopt_port_falls_back_to_default_when_config_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(webapp_config, "DEFAULT_CONFIG_PATH", missing)

    assert _adopt_port() == webapp_config.DEFAULT_PORT
