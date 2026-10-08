"""Boot-time warning for unbound Stream Deck quick actions (issue #857)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.quick_action_config import (
    QuickActionsConfig,
    load_quick_actions_config,
    missing_quick_action_keys,
    warn_missing_quick_action_keys,
)

_LOGGER = "src.quick_action_config"
_SECRET_PLUG = "test-plug-device-id"
_SECRET_AC = "climate.test_unit"


def test_absent_file_warns_naming_every_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "quick_actions.json"
    cfg = load_quick_actions_config(path)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        missing = warn_missing_quick_action_keys(cfg, path)

    assert missing == ["plug_device_id", "ac_climate_entity"]
    (record,) = caplog.records
    assert "is absent" in record.getMessage()
    assert "plug_device_id" in record.getMessage()
    assert "ac_climate_entity" in record.getMessage()


def test_partially_set_file_names_only_the_unset_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "quick_actions.json"
    path.write_text(json.dumps({"plug_device_id": _SECRET_PLUG, "ac_climate_entity": ""}), encoding="utf-8")
    cfg = load_quick_actions_config(path)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        missing = warn_missing_quick_action_keys(cfg, path)

    assert missing == ["ac_climate_entity"]
    (record,) = caplog.records
    message = record.getMessage()
    assert "has unset keys" in message
    assert "ac_climate_entity" in message
    assert "plug_device_id" not in message
    assert _SECRET_PLUG not in message


def test_fully_configured_is_silent_and_never_logs_values(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = QuickActionsConfig(plug_device_id=_SECRET_PLUG, ac_climate_entity=_SECRET_AC)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        missing = warn_missing_quick_action_keys(cfg, tmp_path / "quick_actions.json")

    assert missing == []
    assert caplog.records == []
    assert missing_quick_action_keys(cfg) == []


def test_registry_boot_check_reads_the_config_the_handlers_use(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.webapp.actions_registry as registry

    monkeypatch.setattr(registry, "_quick_actions_config", QuickActionsConfig())

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        registry.warn_unconfigured_quick_actions()

    assert len(caplog.records) == 1
    assert "plug_device_id" in caplog.records[0].getMessage()
