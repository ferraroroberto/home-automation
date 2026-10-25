from __future__ import annotations

from src.presence_roles import load_presence_roles, resolve_person, set_presence_role


def test_presence_role_store_roundtrips(tmp_path) -> None:
    path = tmp_path / "presence_roles.json"

    set_presence_role("roberto", "dad", path=path)
    set_presence_role("ana", "mom", path=path)

    assert load_presence_roles(path=path) == {"roberto": "dad", "ana": "mom"}

    set_presence_role("roberto", "", path=path)
    assert load_presence_roles(path=path) == {"ana": "mom"}


def test_resolve_person_matches_role_case_insensitively() -> None:
    result = resolve_person(
        "Dad",
        roles={"roberto": "dad", "ana": "mom"},
        display_names={},
        known_ids=["roberto", "ana"],
        known_names={},
    )
    assert result == "roberto"


def test_resolve_person_matches_display_name_when_no_role() -> None:
    result = resolve_person(
        "roberto",
        roles={},
        display_names={"iphone-123": "Roberto"},
        known_ids=["iphone-123"],
        known_names={},
    )
    assert result == "iphone-123"


def test_resolve_person_matches_raw_known_name_or_id() -> None:
    result = resolve_person(
        "iphone-123",
        roles={},
        display_names={},
        known_ids=["iphone-123"],
        known_names={"iphone-123": "Roberto's iPhone"},
    )
    assert result == "iphone-123"

    result = resolve_person(
        "Roberto's iPhone",
        roles={},
        display_names={},
        known_ids=["iphone-123"],
        known_names={"iphone-123": "Roberto's iPhone"},
    )
    assert result == "iphone-123"


def test_resolve_person_matches_role_kinship_variants() -> None:
    """Whisper-heard variants of the configured role must resolve (#446)."""

    roles = {"roberto": "dad", "ana": "mom"}
    for spoken in ("mum", "mummy", "mommy", "mama", "mamá", "mami"):
        assert (
            resolve_person(spoken, roles=roles, display_names={}, known_ids=[], known_names={})
            == "ana"
        ), spoken
    for spoken in ("daddy", "papa", "papá", "papi"):
        assert (
            resolve_person(spoken, roles=roles, display_names={}, known_ids=[], known_names={})
            == "roberto"
        ), spoken


def test_resolve_person_matches_variant_stored_role() -> None:
    """A role stored as a variant ("papá") meets the spoken canonical ("dad")."""

    roles = {"roberto": "papá"}
    for spoken in ("dad", "papa", "daddy"):
        assert (
            resolve_person(spoken, roles=roles, display_names={}, known_ids=[], known_names={})
            == "roberto"
        ), spoken


def test_resolve_person_tolerates_doubled_letters_and_accents() -> None:
    """"Anna" ↔ "Ana" and accent-insensitive name matching (#446)."""

    result = resolve_person(
        "Anna",
        roles={},
        display_names={"iphone-123": "Ana"},
        known_ids=["iphone-123"],
        known_names={},
    )
    assert result == "iphone-123"

    result = resolve_person(
        "Ramon",
        roles={},
        display_names={},
        known_ids=["iphone-456"],
        known_names={"iphone-456": "Ramón"},
    )
    assert result == "iphone-456"


def test_resolve_person_returns_none_when_no_match() -> None:
    result = resolve_person(
        "grandma",
        roles={"roberto": "dad"},
        display_names={},
        known_ids=["roberto"],
        known_names={},
    )
    assert result is None
