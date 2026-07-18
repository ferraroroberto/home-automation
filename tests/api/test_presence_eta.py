"""API smoke for the ETA-home voice bridge (issue #470).

``GET /api/presence/eta`` reuses the locator's name/role resolution and Find My
cache, then routes the person's live coordinates to the configured home via
Google Directions. ``fetch_travel_time`` is monkeypatched here — nothing touches
the real Directions API — and every failure mode returns a spoken fallback with
HTTP 200, mirroring the locator's graceful contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.webapp.presence_refresher import PresenceDiagnosticsCache
from src.location_config import LocationConfig
from src.presence_client import PresenceEntity
from src.travel_time import TravelTime


def _away_entity(entity_id: str = "roberto-phone") -> PresenceEntity:
    return PresenceEntity(
        entity_id=entity_id,
        name="Roberto's iPhone",
        model="iPhone",
        device_class="iPhone",
        latitude=41.5,
        longitude=2.1,
        horizontal_accuracy_m=8.0,
        last_seen=datetime.now(timezone.utc),
        battery_level_pct=80,
        battery_status="Charging",
        distance_from_home_m=9000.0,
        at_home=False,
    )


def _wire_common(monkeypatch: pytest.MonkeyPatch, tmp_path, *, entities, people=None) -> None:
    import src.presence_display_names as pdn
    import src.presence_places as pp
    import src.presence_roles as pr

    monkeypatch.setattr(pdn, "DEFAULT_PATH", tmp_path / "presence_display_names.json")
    monkeypatch.setattr(pr, "DEFAULT_PATH", tmp_path / "presence_roles.json")
    monkeypatch.setattr(pp, "PLACES_PATH", tmp_path / "presence_places.json")
    pr.set_presence_role("roberto-phone", "dad")

    monkeypatch.setattr("app.webapp.routers.presence.load_people", lambda: people or {})
    monkeypatch.setattr(
        "app.webapp.routers.presence.get_cache",
        lambda: PresenceDiagnosticsCache(
            entities=entities, refreshed_at=datetime.now(timezone.utc), available=True, reason="ok"
        ),
    )
    monkeypatch.setattr(
        "app.webapp.routers.presence.load_location_config",
        lambda: LocationConfig(lat=41.4, lon=2.15, label="home"),
    )


def _patch_travel(monkeypatch: pytest.MonkeyPatch, result: TravelTime) -> list:
    calls: list = []

    async def fake_fetch(*, origin_lat, origin_lon, dest_lat, dest_lon, **_):
        calls.append((origin_lat, origin_lon, dest_lat, dest_lon))
        return result

    monkeypatch.setattr("app.webapp.routers.presence.fetch_travel_time", fake_fetch)
    return calls


def test_eta_speaks_traffic_time_for_away_person(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_common(monkeypatch, tmp_path, entities=[_away_entity()])
    calls = _patch_travel(
        monkeypatch, TravelTime(available=True, duration_s=1080, duration_text="18 mins")
    )

    body = client.get("/api/presence/eta", params={"who": "dad"}).json()
    assert body["found"] is True
    assert body["eta_minutes"] == 18
    assert body["speech"] == "Roberto's iPhone is about 18 minutes from home in current traffic."
    # Origin = the entity's live coords, destination = the configured home.
    assert calls == [(41.5, 2.1, 41.4, 2.15)]


def test_eta_speaks_spanish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_common(monkeypatch, tmp_path, entities=[_away_entity()])
    _patch_travel(monkeypatch, TravelTime(available=True, duration_s=60, duration_text="1 min"))

    body = client.get("/api/presence/eta", params={"who": "dad", "lang": "es"}).json()
    assert body["speech"] == "Roberto's iPhone está a un minuto de casa con el tráfico actual."


@pytest.mark.parametrize(
    "seconds, lang, expected",
    [
        # < 1h — bare minutes, singular/plural.
        (18 * 60, "en", "X is about 18 minutes from home in current traffic."),
        (60, "en", "X is about 1 minute from home in current traffic."),
        (60, "es", "X está a un minuto de casa con el tráfico actual."),
        (21 * 60, "es", "X está a unos 21 minutos de casa con el tráfico actual."),
        # exact hours — minutes dropped, gender + singular.
        (60 * 60, "en", "X is about 1 hour from home in current traffic."),
        (2 * 3600, "es", "X está a unas 2 horas de casa con el tráfico actual."),
        (60 * 60, "es", "X está a una hora de casa con el tráfico actual."),
        # hours + minutes (the 578-min case that motivated #474).
        (9 * 3600 + 38 * 60, "en", "X is about 9 hours 38 minutes from home in current traffic."),
        (9 * 3600 + 38 * 60, "es", "X está a unas 9 horas y 38 minutos de casa con el tráfico actual."),
        (3600 + 60, "es", "X está a una hora y un minuto de casa con el tráfico actual."),
    ],
)
def test_eta_speech_formats_hours_and_minutes(seconds: int, lang: str, expected: str) -> None:
    from app.webapp.routers.presence import _eta_speech

    assert _eta_speech("X", seconds, lang=lang) == expected


def test_eta_already_home_skips_lookup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src.presence_engine import PersonPresence

    people = {"ana": PersonPresence("ana", "home", datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))}
    _wire_common(monkeypatch, tmp_path, entities=[], people=people)
    calls = _patch_travel(monkeypatch, TravelTime(available=True, duration_s=600))

    body = client.get("/api/presence/eta", params={"who": "ana"}).json()
    assert body["found"] is True
    assert body["eta_minutes"] is None
    assert body["speech"] == "ana is already home."
    assert calls == []  # no routing call when they're already home


def test_eta_unknown_person(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_common(monkeypatch, tmp_path, entities=[])
    _patch_travel(monkeypatch, TravelTime(available=True, duration_s=600))

    body = client.get("/api/presence/eta", params={"who": "grandma"}).json()
    assert body["found"] is False
    assert body["speech"] == "I don't know who grandma is."


def test_eta_home_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_common(monkeypatch, tmp_path, entities=[_away_entity()])
    monkeypatch.setattr("app.webapp.routers.presence.load_location_config", lambda: None)
    calls = _patch_travel(monkeypatch, TravelTime(available=True, duration_s=600))

    body = client.get("/api/presence/eta", params={"who": "dad"}).json()
    assert body["eta_minutes"] is None
    assert body["speech"] == "Home location isn't set, so I can't work out the trip."
    assert calls == []  # no point routing without a destination


def test_eta_falls_back_when_lookup_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _wire_common(monkeypatch, tmp_path, entities=[_away_entity()])
    _patch_travel(monkeypatch, TravelTime(available=False, reason="no_api_key"))

    body = client.get("/api/presence/eta", params={"who": "dad"}).json()
    assert body["eta_minutes"] is None
    assert body["speech"] == "Travel-time lookup isn't set up."
