"""Unit tests for :mod:`src.webapp_config` — the webapp host/port + auth store."""

from __future__ import annotations

from pathlib import Path

import pytest

from src._schedule_store import StoreUnreadableError
from src.webapp_config import WebappConfig, load_webapp_config, save_webapp_config


def test_load_missing_file_is_defaults(tmp_path: Path) -> None:
    assert load_webapp_config(tmp_path / "absent.json") == WebappConfig()


def test_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "webapp_config.json"
    save_webapp_config(WebappConfig(host="127.0.0.1", port=9000, auth_token="tok"), path)
    cfg = load_webapp_config(path)
    assert cfg.host == "127.0.0.1" and cfg.port == 9000 and cfg.auth_token == "tok"


def test_unreadable_file_raises_instead_of_returning_defaults(tmp_path: Path) -> None:
    """Issue #692: corrupt content must not look like "no config saved yet" —
    ``update_webapp_config`` would save the defaults back over a real
    ``auth_token``/``auth_password``."""
    path = tmp_path / "webapp_config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StoreUnreadableError):
        load_webapp_config(path)
