"""API smoke for iCloud entity staleness (issue #483).

``GET /api/presence`` used to hard-code ``stale: False`` for every iCloud/Find
My entity regardless of how old its fix was. It's now computed from
``last_seen`` against a configurable threshold, same shape as the existing
webhook-person computation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceDiagnosticsCache
from src.presence_client import PresenceEntity
from src.presence_engine import PersonPresence


def _icloud_entity(*, last_seen, entity_id: str = "roberto-phone") -> PresenceEntity:
    return PresenceEntity(
        entity_id=entity_id,
        name="Roberto's iPhone",
        model="iPhone",
        device_class="iPhone",
        latitude=41.5,
        longitude=2.1,
        horizontal_accuracy_m=8.0,
        last_seen=last_seen,
        battery_level_pct=80,
        battery_status="Charging",
        distance_from_home_m=9000.0,
        at_home=False,
    )


def _wire_cache(monkeypatch: pytest.MonkeyPatch, tmp_path, *, entities) -> None:
    import src.presence_display_names as pdn
    import src.presence_hidden as ph
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(ph, "DEFAULT_PATH", tmp_path / "presence_hidden.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")

    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=entities, refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )


def test_icloud_entity_with_old_last_seen_is_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _wire_cache(monkeypatch, tmp_path, entities=[_icloud_entity(last_seen=old)])

    body = client.get("/api/presence").json()
    assert body["entities"][0]["stale"] is True


def test_icloud_entity_with_fresh_last_seen_is_not_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fresh = datetime.now(timezone.utc) - timedelta(seconds=10)
    _wire_cache(monkeypatch, tmp_path, entities=[_icloud_entity(last_seen=fresh)])

    body = client.get("/api/presence").json()
    assert body["entities"][0]["stale"] is False


def test_icloud_entity_with_no_last_seen_is_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_cache(monkeypatch, tmp_path, entities=[_icloud_entity(last_seen=None)])

    body = client.get("/api/presence").json()
    assert body["entities"][0]["stale"] is True


def test_webhook_person_stale_behavior_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Webhook-backed people still use ``PresenceAutomationConfig.stale_after_s``,
    untouched by the iCloud staleness computation added in #483."""
    import src.presence_display_names as pdn
    import src.presence_engine as pe
    import src.presence_hidden as ph
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(ph, "DEFAULT_PATH", tmp_path / "presence_hidden.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    monkeypatch.setattr(pe, "AUTOMATION_PATH", tmp_path / "presence_automation.json")

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(
        "app.webapp.routers.presence.load_people",
        lambda: {"ana": PersonPresence("ana", "home", old)},
    )
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    body = client.get("/api/presence").json()
    person = next(e for e in body["entities"] if e["entity_id"] == "ana")
    assert person["stale"] is True
