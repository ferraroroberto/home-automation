"""API smoke for the base presence endpoint + entity rename/hide.

``GET /api/presence`` serialises the cached Find My diagnostics snapshot
(including partial per-account outages); the rename/hide routes persist
their overrides. iCloud is never touched — the diagnostics cache is faked.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceAccountStatus, PresenceDiagnosticsCache
from src.presence_client import PresenceEntity


def test_presence_route_serializes_find_my_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``GET /api/presence`` returns cached diagnostics without real iCloud I/O."""
    entities = [
        PresenceEntity(
            entity_id="home-phone",
            name="Home Phone",
            model="iPhone",
            device_class="iPhone",
            latitude=0.0,
            longitude=0.0,
            horizontal_accuracy_m=8.0,
            last_seen=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
            battery_level_pct=80,
            battery_status="Charging",
            distance_from_home_m=50.0,
            at_home=True,
        ),
        PresenceEntity(
            entity_id="away-phone",
            name="Away Phone",
            model="iPhone",
            device_class="iPhone",
            latitude=0.1,
            longitude=0.0,
            horizontal_accuracy_m=12.0,
            last_seen=None,
            battery_level_pct=None,
            battery_status=None,
            distance_from_home_m=1000.0,
            at_home=False,
        ),
        PresenceEntity(
            entity_id="tag",
            name="Keys",
            model="AirTag",
            device_class="Accessory",
            latitude=None,
            longitude=None,
            horizontal_accuracy_m=None,
            last_seen=None,
            battery_level_pct=None,
            battery_status=None,
        ),
    ]

    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=entities,
            refreshed_at=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            available=True,
            reason="ok",
            home_radius_m=200,
        ),
    )

    resp = client.get("/api/presence")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["total_count"] == 3
    assert body["located_count"] == 2
    assert body["home_count"] == 1
    assert body["away_count"] == 1
    assert body["unknown_count"] == 1
    assert body["all_away"] is False
    assert body["home_radius_m"] == 200
    assert body["entities"][0]["last_seen"] == "2026-06-22T10:00:00+00:00"
    assert body["diagnostics"]["available"] is True


def test_presence_route_returns_unavailable_when_icloud_needs_2fa(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            available=False,
            reason="2fa_required",
            detail="iCloud requires 2FA",
        ),
    )

    resp = client.get("/api/presence")
    assert resp.status_code == 200
    assert resp.json()["diagnostics"]["reason"] == "2fa_required"


def test_presence_route_surfaces_per_account_partial_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial two-account outage surfaces per-account diagnostics (#478)."""
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[],
            refreshed_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            available=True,
            reason="partial",
            detail="1 of 2 iCloud accounts need re-auth (account 2): ...",
            home_radius_m=200,
            accounts=[
                PresenceAccountStatus("1", True, "ok", "", 2),
                PresenceAccountStatus("2", False, "2fa_required", "needs 2FA", 0),
            ],
        ),
    )

    diag = client.get("/api/presence").json()["diagnostics"]
    assert diag["reason"] == "partial"
    assert diag["available"] is True
    assert [a["label"] for a in diag["accounts"]] == ["1", "2"]
    assert diag["accounts"][1]["available"] is False
    assert diag["accounts"][1]["reason"] == "2fa_required"


def test_presence_hidden_and_display_name_persist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_display_names as pdn
    import src.presence_hidden as ph

    names = tmp_path / "presence_display_names.json"
    hidden = tmp_path / "presence_hidden.json"
    monkeypatch.setattr(pdn, "DEFAULT_PATH", names)
    monkeypatch.setattr(ph, "DEFAULT_PATH", hidden)

    resp = client.put(
        "/api/presence/entity-display-name",
        json={"entity_id": "ana", "display_name": "Ana"},
    )
    assert resp.status_code == 200
    assert pdn.load_presence_display_names() == {"ana": "Ana"}

    resp = client.put(
        "/api/presence/entity-hidden",
        json={"entity_id": "ana", "hidden": True},
    )
    assert resp.status_code == 200
    assert ph.load_hidden_presence_ids() == {"ana"}

    unsafe_id = "Find/My+Accessory/2"
    resp = client.put(
        "/api/presence/entity-hidden",
        json={"entity_id": unsafe_id, "hidden": True},
    )
    assert resp.status_code == 200
    assert unsafe_id in ph.load_hidden_presence_ids()
