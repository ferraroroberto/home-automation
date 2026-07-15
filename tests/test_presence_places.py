from __future__ import annotations

from src.presence_places import (
    PresencePlace,
    UNKNOWN_PLACE,
    load_presence_places,
    resolve_place,
    set_presence_places,
)


def test_presence_places_store_normalizes_and_persists(tmp_path) -> None:
    path = tmp_path / "presence_places.json"

    places = set_presence_places(
        [
            {"id": " the gym ", "label": "the gym", "lat": "12.5", "lon": "-1.5", "radius_m": "200"},
            {"id": "", "label": "", "lat": 999, "lon": -999, "radius_m": "bad"},
        ],
        path=path,
    )

    assert places[0].id == "the-gym"
    assert places[0].label == "the gym"
    assert places[0].lat == 12.5
    assert places[0].lon == -1.5
    assert places[0].radius_m == 200.0
    assert places[1].id == "place-2"
    assert places[1].label == "place-2"
    assert places[1].lat == 90.0
    assert places[1].lon == -180.0
    assert places[1].radius_m == 150.0
    assert load_presence_places(path=path) == places


def test_resolve_place_prefers_closest_place_within_radius() -> None:
    gym = PresencePlace(id="gym", label="the gym", lat=0.0, lon=0.0, radius_m=150.0)
    work = PresencePlace(id="work", label="Roberto's work", lat=0.01, lon=0.01, radius_m=150.0)

    result = resolve_place(
        latitude=0.0,
        longitude=0.0,
        at_home=False,
        has_location=True,
        places=[gym, work],
    )

    assert result == "the gym"


def test_resolve_place_falls_back_to_home_when_no_place_matches() -> None:
    gym = PresencePlace(id="gym", label="the gym", lat=10.0, lon=10.0, radius_m=100.0)

    result = resolve_place(
        latitude=0.0, longitude=0.0, at_home=True, has_location=True, places=[gym]
    )

    assert result == "Home"


def test_resolve_place_falls_back_to_away_when_located_but_unmatched() -> None:
    gym = PresencePlace(id="gym", label="the gym", lat=10.0, lon=10.0, radius_m=100.0)

    result = resolve_place(
        latitude=0.0, longitude=0.0, at_home=False, has_location=True, places=[gym]
    )

    assert result == "Away"


def test_resolve_place_reports_unknown_when_no_location() -> None:
    result = resolve_place(
        latitude=None, longitude=None, at_home=False, has_location=False, places=[]
    )

    assert result == UNKNOWN_PLACE
