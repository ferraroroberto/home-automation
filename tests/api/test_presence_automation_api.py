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
