"""Unit tests for the Wi-Fi walk-test sample store (issue #547).

Pure logic against a tmp-path SQLite DB — no network, no AP, no router. What
matters here is that a sample round-trips intact, that the per-room summary
picks the *latest* reading while remembering the extremes, that "the device was
on neither radio" survives as its own state rather than collapsing into a blank,
and that the weakest room sorts to the top (the whole point of a walk test).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.network_survey import (
    SOURCE_NOT_FOUND,
    SOURCE_UNKNOWN,
    delete_room,
    delete_sample,
    known_rooms,
    load_samples,
    record_sample,
    room_summary,
    _PRUNE_AFTER_S,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "survey.sqlite3"


def _record(db: Path, room: str, signal, **kw):
    kw.setdefault("source", "ap" if signal is not None else SOURCE_NOT_FOUND)
    return record_sample(room=room, mac="AA:BB:CC:00:00:01", signal=signal, path=db, **kw)


def test_record_round_trips_every_field(db: Path) -> None:
    stored = record_sample(
        room="Kitchen",
        mac="aa:bb:cc:00:00:01",
        signal=72,
        link_rate=390,
        band="5GHz",
        ssid="TestNet",
        source="ap",
        rtt_ms=12.5,
        jitter_ms=1.5,
        loss_pct=0.0,
        throughput_mbps=180.4,
        now=1_700_000_000,
        path=db,
    )
    assert stored["room"] == "Kitchen"
    assert stored["mac"] == "AA:BB:CC:00:00:01"  # normalised upper-case
    assert stored["signal"] == 72
    assert stored["link_rate"] == 390
    assert stored["band"] == "5GHz"
    assert stored["ssid"] == "TestNet"
    assert stored["source"] == "ap"
    assert stored["found"] is True
    assert stored["rtt_ms"] == 12.5
    assert stored["throughput_mbps"] == 180.4
    assert stored["recorded_at"] == 1_700_000_000

    assert load_samples(path=db) == [stored]


def test_room_label_is_whitespace_normalised(db: Path) -> None:
    _record(db, "  Living   Room ", 60)
    _record(db, "Living Room", 55)
    assert known_rooms(path=db) == ["Living Room"]
    assert len(room_summary(path=db)) == 1


@pytest.mark.parametrize("room,mac", [("", "AA:BB:CC:00:00:01"), ("Kitchen", "  ")])
def test_empty_room_or_mac_is_rejected(db: Path, room: str, mac: str) -> None:
    with pytest.raises(ValueError):
        record_sample(room=room, mac=mac, path=db)


def test_not_found_is_recorded_as_its_own_state(db: Path) -> None:
    """A MAC on neither radio must not look like an ordinary reading.

    This is the case the UI has to distinguish: standing in a dead spot is a
    real, useful result, and a null signal with no marker would render as a
    blank cell indistinguishable from a measurement that simply failed.
    """
    stored = record_sample(
        room="Garage", mac="AA:BB:CC:00:00:01", source=SOURCE_NOT_FOUND, path=db
    )
    assert stored["signal"] is None
    assert stored["source"] == SOURCE_NOT_FOUND
    assert stored["found"] is False

    summary = room_summary(path=db)[0]
    assert summary["room"] == "Garage"
    assert summary["last_found"] is False
    assert summary["last_signal"] is None


def test_summary_uses_latest_reading_and_remembers_extremes(db: Path) -> None:
    _record(db, "Study", 40, now=100)
    _record(db, "Study", 90, now=200)
    _record(db, "Study", 65, now=300)

    summary = room_summary(path=db)
    assert len(summary) == 1
    entry = summary[0]
    assert entry["count"] == 3
    assert entry["last_signal"] == 65  # latest, not best or mean
    assert entry["best_signal"] == 90
    assert entry["worst_signal"] == 40
    assert entry["last_recorded_at"] == 300


def test_summary_sorts_weakest_first_with_dead_spots_ahead(db: Path) -> None:
    _record(db, "Office", 88, now=100)
    _record(db, "Bedroom", 45, now=100)
    _record(db, "Attic", None, now=100)  # on neither radio

    assert [r["room"] for r in room_summary(path=db)] == ["Attic", "Bedroom", "Office"]


def test_unreadable_sources_are_unknown_not_a_dead_zone(db: Path) -> None:
    """An unreachable AP is an outage, not a coverage result.

    Both states have a null signal and `found=False`, but only `not_found` is a
    claim about the radio environment — folding an unusable probe into it would
    report a dead zone the walk test never actually observed.
    """
    stored = record_sample(
        room="Loft", mac="AA:BB:CC:00:00:01", source=SOURCE_UNKNOWN, path=db
    )
    assert stored["signal"] is None
    assert stored["found"] is False
    assert stored["source"] == SOURCE_UNKNOWN
    assert stored["source"] != SOURCE_NOT_FOUND


def test_unknown_rooms_sort_last_not_alongside_dead_zones(db: Path) -> None:
    """A room that measured nothing must not head a coverage report."""
    _record(db, "Office", 88, now=100)
    _record(db, "Bedroom", 45, now=100)
    _record(db, "Attic", None, now=100)  # genuinely on neither radio
    record_sample(
        room="Loft", mac="AA:BB:CC:00:00:01", source=SOURCE_UNKNOWN, now=100, path=db
    )

    assert [r["room"] for r in room_summary(path=db)] == [
        "Attic", "Bedroom", "Office", "Loft"
    ]


def test_source_defaults_to_unknown_not_not_found(db: Path) -> None:
    """A caller that supplies no source has not proved the client is off the air."""
    stored = record_sample(room="Hall", mac="AA:BB:CC:00:00:01", path=db)
    assert stored["source"] == SOURCE_UNKNOWN


def test_known_rooms_is_deduped_and_sorted(db: Path) -> None:
    _record(db, "Kitchen", 70)
    _record(db, "Attic", 30)
    _record(db, "Kitchen", 68)
    assert known_rooms(path=db) == ["Attic", "Kitchen"]


def test_delete_sample_removes_only_that_row(db: Path) -> None:
    first = _record(db, "Kitchen", 70, now=100)
    _record(db, "Kitchen", 60, now=200)

    assert delete_sample(first["id"], path=db) is True
    remaining = load_samples(path=db)
    assert len(remaining) == 1
    assert remaining[0]["signal"] == 60
    # A second delete of the same id is a no-op, not an error.
    assert delete_sample(first["id"], path=db) is False


def test_delete_room_removes_the_whole_room_only(db: Path) -> None:
    _record(db, "Kitchen", 70)
    _record(db, "Kitchen", 60)
    _record(db, "Office", 80)

    assert delete_room("Kitchen", path=db) == 2
    assert [s["room"] for s in load_samples(path=db)] == ["Office"]
    # Free-form label with a slash — the reason deletes are body-carried, not
    # path-carried, in the API layer.
    _record(db, "Garage / Cellar", 20)
    assert delete_room("Garage / Cellar", path=db) == 1


def test_samples_past_retention_are_pruned_on_write(db: Path) -> None:
    now = 1_700_000_000
    _record(db, "Kitchen", 70, now=now - _PRUNE_AFTER_S - 1)
    assert len(load_samples(path=db)) == 1

    _record(db, "Kitchen", 65, now=now)
    remaining = load_samples(path=db)
    assert len(remaining) == 1
    assert remaining[0]["recorded_at"] == now


def test_reads_are_empty_not_an_error_on_a_fresh_db(db: Path) -> None:
    assert load_samples(path=db) == []
    assert room_summary(path=db) == []
    assert known_rooms(path=db) == []
