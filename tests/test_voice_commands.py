"""Catalogue integrity for the voice cheat sheet (issue #437).

The catalogue is hand-curated prose, so these are the invariants the PWA card
relies on rather than a re-statement of the content: shape (the renderer walks
groups -> commands -> phrasings), unique ids, examples that are actually
sayable, and — the one that matters most — that the disarm code stays a
placeholder. The repo is public and the PWA is reachable beyond loopback.
"""

from __future__ import annotations

import re

from src.voice_commands import (
    VOICE_COMMAND_GROUPS,
    WAKE_WORD_EN,
    WAKE_WORD_ES,
    load_voice_commands,
)

WAKE_WORDS = {WAKE_WORD_EN, WAKE_WORD_ES}


def test_groups_cover_every_wired_feature() -> None:
    assert [g.id for g in VOICE_COMMAND_GROUPS] == [
        "alarm",
        "wake-alarms",
        "locator",
        "grocery",
        "built-ins",
    ]


def test_ids_are_unique() -> None:
    group_ids = [g.id for g in VOICE_COMMAND_GROUPS]
    assert len(group_ids) == len(set(group_ids))

    command_ids = [c.id for g in VOICE_COMMAND_GROUPS for c in g.commands]
    assert len(command_ids) == len(set(command_ids))


def test_every_command_is_renderable() -> None:
    for group in VOICE_COMMAND_GROUPS:
        assert group.title and group.summary and group.icon
        assert group.commands, f"{group.id} has no commands"
        for command in group.commands:
            assert command.intent and command.reply, command.id
            assert command.phrasings, f"{command.id} has no phrasings"
            for phrasing in command.phrasings:
                assert phrasing.lang in {"en", "es"}, phrasing.lang
                assert phrasing.wake_word in WAKE_WORDS, phrasing.wake_word
                assert phrasing.phrases, f"{command.id} has an empty phrase list"


def test_examples_lead_with_their_wake_word() -> None:
    """The example is the thing a user reads aloud, so it must be complete."""

    for group in VOICE_COMMAND_GROUPS:
        for command in group.commands:
            for phrasing in command.phrasings:
                assert phrasing.example.startswith(phrasing.wake_word + ", "), (
                    f"{command.id} ({phrasing.lang}): {phrasing.example!r}"
                )


def test_example_contains_one_of_its_phrases() -> None:
    """The card hides any phrase its example already shows (voice-commands.js).

    An example that matches none of its phrases would list every phrase twice;
    one that matches all of them would list none.
    """

    for group in VOICE_COMMAND_GROUPS:
        for command in group.commands:
            for phrasing in command.phrasings:
                matched = [p for p in phrasing.phrases if p in phrasing.example]
                assert len(matched) == 1, (
                    f"{command.id} ({phrasing.lang}) example matches {len(matched)} "
                    f"of its phrases, expected exactly 1: {phrasing.example!r}"
                )


def test_locator_is_bilingual_and_grocery_is_spanish_only() -> None:
    """The two cases that drive the card's per-phrasing language chip."""

    locator = next(g for g in VOICE_COMMAND_GROUPS if g.id == "locator")
    langs = {p.lang for c in locator.commands for p in c.phrasings}
    assert langs == {"en", "es"}

    grocery = next(g for g in VOICE_COMMAND_GROUPS if g.id == "grocery")
    grocery_langs = {p.lang for c in grocery.commands for p in c.phrasings}
    assert grocery_langs == {"es"}
    assert all(
        p.wake_word == WAKE_WORD_ES for c in grocery.commands for p in c.phrasings
    )


def test_disarm_never_publishes_a_code() -> None:
    """Only the ``<your code>`` placeholder the voice-pe README already ships.

    A real code pasted in here would be served to every client of a
    beyond-loopback PWA and committed to a public repo.
    """

    alarm = next(g for g in VOICE_COMMAND_GROUPS if g.id == "alarm")
    disarm = next(c for c in alarm.commands if c.id == "alarm-disarm")
    for phrasing in disarm.phrasings:
        for text in (*phrasing.phrases, phrasing.example):
            assert "<your code>" in text, text


def test_no_bare_digits_leak_into_the_alarm_group() -> None:
    """Belt-and-braces on the group where a leaked code would actually matter."""

    alarm = next(g for g in VOICE_COMMAND_GROUPS if g.id == "alarm")
    texts = [
        text
        for command in alarm.commands
        for phrasing in command.phrasings
        for text in (*phrasing.phrases, phrasing.example, command.reply)
    ]
    for text in texts:
        assert not re.search(r"\d", text), f"digit in alarm cheat sheet: {text!r}"


def test_load_voice_commands_is_json_ready() -> None:
    groups = load_voice_commands()
    assert isinstance(groups, list)
    assert [g["id"] for g in groups] == [g.id for g in VOICE_COMMAND_GROUPS]

    alarm = groups[0]
    assert isinstance(alarm["commands"], list)
    assert isinstance(alarm["notes"], list)
    phrasing = alarm["commands"][0]["phrasings"][0]
    assert isinstance(phrasing["phrases"], list)
    assert phrasing["wake_word"] == WAKE_WORD_EN
