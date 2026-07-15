"""API smoke for the presence locator (issue #438).

``GET/PUT /api/presence/places`` edits the named-place list; ``PUT
/api/presence/role`` sets a household-role alias; ``GET /api/presence/locate``
is the voice-bridge endpoint. All cloud/Find My reads are monkeypatched —
never touches real iCloud.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceDiagnosticsCache
from src.presence_client import PresenceEntity


def test_presence_places_endpoint_persists_normalized_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_places as pp

    store = tmp_path / "presence_places.json"
    monkeypatch.setattr(pp, "PLACES_PATH", store)

    resp = client.get("/api/presence/places")
    assert resp.status_code == 200
    assert resp.json() == {"places": [], "count": 0}

    resp = client.put(
        "/api/presence/places",
        json={
            "places": [
                {"id": "the gym", "label": "the gym", "lat": 1.0, "lon": 2.0, "radius_m": 100},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["places"][0]["id"] == "the-gym"
    assert pp.load_presence_places(path=store)[0].label == "the gym"

    resp = client.put("/api/presence/places", json={"places": "nope"})
    assert resp.status_code == 400


def test_presence_route_surfaces_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``GET /api/presence`` merges the role alongside display_name/hidden."""
    import src.presence_display_names as pdn
    import src.presence_hidden as ph
    import src.presence_roles as pr
    from src.presence_engine import PersonPresence

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(ph, "DEFAULT_PATH", tmp_path / "presence_hidden.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    pr.set_presence_role("ana", "mom")

    monkeypatch.setattr(
        "app.webapp.routers.presence.load_people",
        lambda: {"ana": PersonPresence("ana", "home", datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))},
    )
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    body = client.get("/api/presence").json()
    assert body["entities"][0]["role"] == "mom"
    assert body["entities"][0]["current_place"] == "Home"


def test_presence_role_endpoint_persists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_roles as pr

    store = tmp_path / "presence_roles.json"
    monkeypatch.setattr(pr, "DEFAULT_PATH", store)

    resp = client.put("/api/presence/role", json={"entity_id": "roberto", "role": "dad"})
    assert resp.status_code == 200
    assert resp.json() == {"entity_id": "roberto", "role": "dad"}
    assert pr.load_presence_roles(path=store) == {"roberto": "dad"}

    resp = client.put("/api/presence/role", json={"entity_id": "roberto", "role": ""})
    assert resp.status_code == 200
    assert resp.json()["role"] is None
    assert pr.load_presence_roles(path=store) == {}


def test_presence_locate_resolves_role_to_named_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("roberto-phone", "dad")
    pp.set_presence_places([{"id": "gym", "label": "the gym", "lat": 0.0, "lon": 0.0, "radius_m": 150}])

    entity = PresenceEntity(
        entity_id="roberto-phone",
        name="Roberto's iPhone",
        model="iPhone",
        device_class="iPhone",
        latitude=0.0,
        longitude=0.0,
        horizontal_accuracy_m=8.0,
        last_seen=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        battery_level_pct=80,
        battery_status="Charging",
        distance_from_home_m=5000.0,
        at_home=False,
    )
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[entity], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    resp = client.get("/api/presence/locate", params={"who": "dad"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "found": True,
        "entity_id": "roberto-phone",
        "name": "Roberto's iPhone",
        "place": "the gym",
        "speech": "Roberto's iPhone is at the gym.",
    }

    # Given name resolves identically via the display-name/raw-name fallback.
    resp2 = client.get("/api/presence/locate", params={"who": "Roberto's iPhone"})
    assert resp2.json()["place"] == "the gym"


def test_presence_locate_reports_home_and_unknown_person(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr
    from src.presence_engine import PersonPresence

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("ana", "mom")

    monkeypatch.setattr(
        "app.webapp.routers.presence.load_people",
        lambda: {"ana": PersonPresence("ana", "home", datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))},
    )
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    resp = client.get("/api/presence/locate", params={"who": "mom"})
    assert resp.json() == {
        "found": True,
        "entity_id": "ana",
        "name": "ana",
        "place": "Home",
        "speech": "ana is home.",
    }

    resp = client.get("/api/presence/locate", params={"who": "grandma"})
    body = resp.json()
    assert body["found"] is False
    assert body["speech"] == "I don't know who grandma is."


def test_presence_locate_refreshes_stale_cache_on_demand(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A stale (or never-refreshed) cache triggers one bounded on-demand refresh (#442)."""
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("roberto-phone", "dad")

    fresh_entity = PresenceEntity(
        entity_id="roberto-phone",
        name="Roberto's iPhone",
        model="iPhone",
        device_class="iPhone",
        latitude=0.0,
        longitude=0.0,
        horizontal_accuracy_m=8.0,
        last_seen=datetime.now(timezone.utc),
        battery_level_pct=80,
        battery_status="Charging",
        distance_from_home_m=0.0,
        at_home=True,
    )
    fresh_cache = PresenceDiagnosticsCache(
        entities=[fresh_entity], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
    )

    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    # Never refreshed — the endpoint must call refresh_once() before resolving.
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(entities=[], available=False, reason="not_refreshed"),
    )

    calls: list[int] = []

    async def fake_refresh_once() -> PresenceDiagnosticsCache:
        calls.append(1)
        return fresh_cache

    monkeypatch.setattr("app.webapp.routers.presence.refresh_once", fake_refresh_once)

    resp = client.get("/api/presence/locate", params={"who": "dad"})
    assert resp.status_code == 200
    assert resp.json()["place"] == "Home"
    assert calls == [1]


def test_presence_locate_falls_back_to_cache_on_refresh_timeout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A hung on-demand refresh times out and falls back to the existing cache (#442)."""
    import asyncio

    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr
    from src.presence_engine import PersonPresence

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    monkeypatch.setenv("PRESENCE_LOCATE_REFRESH_TIMEOUT_S", "1")

    monkeypatch.setattr(
        "app.webapp.routers.presence.load_people",
        lambda: {"ana": PersonPresence("ana", "home", datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))},
    )
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(entities=[], available=False, reason="not_refreshed"),
    )

    async def hung_refresh_once() -> PresenceDiagnosticsCache:
        await asyncio.sleep(5)
        raise AssertionError("should have timed out before completing")

    monkeypatch.setattr("app.webapp.routers.presence.refresh_once", hung_refresh_once)

    resp = client.get("/api/presence/locate", params={"who": "ana"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["place"] == "Home"  # fell back to the webhook person; cache untouched


def test_presence_locate_reports_broken_source_for_icloud_only_alias(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A role known only via Find My, with the diagnostics refresher down, fails loud
    instead of a generic "unknown location" or a raw KeyError (#442)."""
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("roberto-phone", "dad")
    pdn.set_presence_display_name("roberto-phone", "Roberto's iPhone")

    broken_cache = PresenceDiagnosticsCache(
        entities=[], refreshed_at=datetime.now(timezone.utc), available=False, reason="2fa_required"
    )
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr("app.webapp.routers.presence.get_cache", lambda: broken_cache)

    resp = client.get("/api/presence/locate", params={"who": "dad"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "found": True,
        "entity_id": "roberto-phone",
        "name": "Roberto's iPhone",
        "place": "Away — location unknown",
        "speech": "Roberto's iPhone's location tracking needs iCloud re-authentication.",
    }


def _icloud_entity(entity_id: str, *, lat: float, lon: float) -> PresenceEntity:
    return PresenceEntity(
        entity_id=entity_id,
        name="Ana's iPhone",
        model="iPhone",
        device_class="iPhone",
        latitude=lat,
        longitude=lon,
        horizontal_accuracy_m=8.0,
        last_seen=datetime.now(timezone.utc),
        battery_level_pct=80,
        battery_status="Charging",
        distance_from_home_m=5000.0,
        at_home=False,
    )


def test_presence_locate_reverse_geocodes_unmatched_away_location(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A located entity that doesn't match any configured place gets a real
    place name via reverse geocoding instead of a bare "Away" (#442)."""
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("ana-phone", "mom")

    entity = _icloud_entity("ana-phone", lat=41.48, lon=2.06)
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[entity], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    calls = []

    async def fake_reverse_geocode(lat: float, lon: float):
        calls.append((lat, lon))
        return {"available": True, "label": "Gran Via, Barcelona"}

    monkeypatch.setattr("app.webapp.routers.presence._reverse_geocode", fake_reverse_geocode)

    resp = client.get("/api/presence/locate", params={"who": "mom"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "found": True,
        "entity_id": "ana-phone",
        "name": "Ana's iPhone",
        "place": "Gran Via, Barcelona",
        "speech": "Ana's iPhone is at Gran Via, Barcelona.",
    }
    assert calls == [(41.48, 2.06)]


def test_presence_locate_falls_back_to_generic_away_when_geocode_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A failed/unavailable reverse geocode degrades to the existing generic
    "away" speech rather than erroring (#442)."""
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("ana-phone", "mom")

    entity = _icloud_entity("ana-phone", lat=41.48, lon=2.06)
    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=[entity], refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )

    async def failing_reverse_geocode(lat: float, lon: float):
        return {"available": False, "label": "", "detail": "Nominatim HTTP 503"}

    monkeypatch.setattr("app.webapp.routers.presence._reverse_geocode", failing_reverse_geocode)

    resp = client.get("/api/presence/locate", params={"who": "mom"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["place"] == "Away"
    assert body["speech"] == "Ana's iPhone is away — I don't know exactly where."
