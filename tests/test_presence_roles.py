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


def test_resolve_person_returns_none_when_no_match() -> None:
    result = resolve_person(
        "grandma",
        roles={"roberto": "dad"},
        display_names={},
        known_ids=["roberto"],
        known_names={},
    )
    assert result is None
