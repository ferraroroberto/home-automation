"""The PWA's shared shells stay the only implementation.

``view-state.js`` owns the "load and poll while on one tab" gate
(``createTabPollGate``) and ``sheet.js`` owns the dialog shell (×, backdrop,
Esc, focus return). A module that hand-writes either drifts from the shared
one — two dialogs once lost focus return that way — so these scans fail the
moment a copy appears.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"

# Modules that legitimately talk to <dialog> directly: the feature-detect
# itself, the shell built on it, and the Promise-returning confirm prompt.
DIALOG_PRIMITIVE_USERS = {"dialog.js", "sheet.js", "confirm.js"}
SHEET_OWNERS = {"sheet.js", "confirm.js"}

# `createPoller(load)` followed by an `on*Tab` that is exactly
# "load(); schedule(POLL_MS)" on one tab, else "schedule(0)".
HAND_WRITTEN_POLL_GATE = re.compile(
    r"createPoller\((?P<load>\w+)\)[\s\S]*?"
    r"if \(tab === [^)]+\) \{\s*(?P=load)\(\);\s*schedule\(POLL_MS\);\s*"
    r"\} else \{\s*schedule\(0\);\s*\}"
)
BACKDROP_CLICK = re.compile(r"ev\.target === [\w.]*[dD]ialog\b")
DIALOG_PRIMITIVE_IMPORT = re.compile(r"\b(?:openDialog|closeDialog)\b")


def _modules() -> List[Path]:
    return [
        path
        for path in sorted(STATIC.rglob("*.js"))
        if "_vendored" not in path.parts and "vendor" not in path.parts
    ]


def test_no_hand_written_tab_poll_gate() -> None:
    offenders = [
        str(path.relative_to(STATIC))
        for path in _modules()
        if HAND_WRITTEN_POLL_GATE.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "hand-written load-and-poll tab gate; use createTabPollGate: " + ", ".join(offenders)
    )


def test_dialogs_go_through_sheet() -> None:
    offenders = [
        str(path.relative_to(STATIC))
        for path in _modules()
        if path.name not in DIALOG_PRIMITIVE_USERS
        and DIALOG_PRIMITIVE_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "openDialog/closeDialog used outside the shared shell; build on sheet(): "
        + ", ".join(offenders)
    )


def test_no_hand_wired_backdrop_close() -> None:
    offenders = [
        str(path.relative_to(STATIC))
        for path in _modules()
        if path.name not in SHEET_OWNERS
        and BACKDROP_CLICK.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "hand-wired dialog backdrop close; sheet() owns it: " + ", ".join(offenders)
    )
