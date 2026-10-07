from __future__ import annotations

import pytest

from src._env import _env_bool, _env_float, _env_int


def test_blank_and_unset_fall_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("KNOB", raising=False)
    assert _env_int("KNOB", 7) == 7
    monkeypatch.setenv("KNOB", "   ")
    assert _env_int("KNOB", 7) == 7
    assert _env_float("KNOB", 1.5) == 1.5
    assert _env_bool("KNOB", True) is True


def test_invalid_values_warn_and_default(monkeypatch, caplog) -> None:
    monkeypatch.setenv("KNOB", "abc")
    with caplog.at_level("WARNING"):
        assert _env_int("KNOB", 7) == 7
        assert _env_float("KNOB", 1.5) == 1.5
    assert caplog.text.count("Invalid KNOB=abc") == 2


def test_valid_values_parse(monkeypatch) -> None:
    monkeypatch.setenv("KNOB", " 12 ")
    assert _env_int("KNOB", 7) == 12
    monkeypatch.setenv("KNOB", "0.25")
    assert _env_float("KNOB", 1.5) == 0.25
    monkeypatch.setenv("KNOB", "Yes")
    assert _env_bool("KNOB", False) is True
    monkeypatch.setenv("KNOB", "off")
    assert _env_bool("KNOB", True) is False


@pytest.mark.parametrize("non_negative,expected", [(False, -3), (True, 7)])
def test_negative_int_only_rejected_when_asked(monkeypatch, non_negative, expected) -> None:
    monkeypatch.setenv("KNOB", "-3")
    assert _env_int("KNOB", 7, non_negative=non_negative) == expected
