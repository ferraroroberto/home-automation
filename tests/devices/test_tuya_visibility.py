"""Tuya (Smart Life) plug/blind hidden-store helper tests."""

from __future__ import annotations

from src.tuya_hidden import load_hidden_tuya_ids, set_tuya_hidden


def test_tuya_hidden_round_trips(tmp_path) -> None:
    store = tmp_path / "tuya_hidden.json"

    # Absent file is not an error — no devices hidden.
    assert load_hidden_tuya_ids(store) == set()

    set_tuya_hidden("plug-1", True, path=store)
    set_tuya_hidden("blind-2", True, path=store)
    assert load_hidden_tuya_ids(store) == {"plug-1", "blind-2"}

    # Unhiding drops the key, leaving the rest intact.
    set_tuya_hidden("plug-1", False, path=store)
    assert load_hidden_tuya_ids(store) == {"blind-2"}
