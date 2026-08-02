"""API smoke for the auto-arm block diagnostic (issue #531).

``GET /api/presence/automation`` now surfaces *why* presence-driven auto-arm
hasn't fired when it's stuck waiting on a still-home tracked person, instead
of that failing silently with no trace anywhere the user can see.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _wire_state_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import src.presence_engine as pe

    monkeypatch.setattr(pe, "STATE_PATH", tmp_path / "presence_state.json")


def test_presence_automation_defaults_to_not_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_state_path(monkeypatch, tmp_path)

    body = client.get("/api/presence/automation").json()

    assert body["arm_blocked"] is False
    assert body["arm_blocked_person_ids"] == []
    assert body["arm_blocked_since"] is None


def test_presence_automation_surfaces_persisted_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_engine as pe

    _wire_state_path(monkeypatch, tmp_path)
    since = datetime(2026, 7, 25, 20, 21, tzinfo=timezone.utc)
    pe.set_arm_block(pe.PresenceBlock(key="block:ana:x", blocking_person_ids=("ana",), since=since))

    body = client.get("/api/presence/automation").json()

    assert body["arm_blocked"] is True
    assert body["arm_blocked_person_ids"] == ["ana"]
    assert body["arm_blocked_since"] == since.isoformat()


def test_put_automation_preserves_fields_the_pwa_does_not_send(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A save must not reset the knobs the PWA form has no input for (#598).

    ``static/presence-automation.js`` only ever posts the four toggle/number
    fields, so rebuilding the config from bare defaults silently wiped
    ``arm_action`` / ``disarm_action`` and would have wiped the new
    ``disarm_max_age_s`` safety bound every time a toggle was flipped.
    """
    import src.presence_engine as pe

    config_path = tmp_path / "presence_automation.json"
    monkeypatch.setattr(pe, "AUTOMATION_PATH", config_path)
    _wire_state_path(monkeypatch, tmp_path)
    pe.save_automation_config(
        pe.PresenceAutomationConfig(
            auto_arm_enabled=False,
            auto_disarm_enabled=False,
            arm_action="perimeter",
            disarm_action="disarm",
            disarm_max_age_s=300,
        )
    )

    body = client.put(
        "/api/presence/automation",
        json={
            "auto_arm_enabled": True,
            "arm_away_after_s": 600,
            "stale_after_s": 1800,
            "auto_disarm_enabled": True,
        },
    ).json()

    # The payload's own fields are applied...
    assert body["auto_arm_enabled"] is True
    assert body["auto_disarm_enabled"] is True
    assert body["arm_away_after_s"] == 600
    # ...and the ones it never carries survive, on disk as well as in the reply.
    assert body["arm_action"] == "perimeter"
    assert body["disarm_max_age_s"] == 300
    assert pe.load_automation_config(config_path).disarm_max_age_s == 300
    assert pe.load_automation_config(config_path).arm_action == "perimeter"
