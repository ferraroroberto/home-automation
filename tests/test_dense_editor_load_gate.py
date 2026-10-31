"""Every dense list editor must refuse to save a list it failed to load.

Each save PUTs the whole list, so an editor that stays usable after its GET
failed overwrites the stored list with the one-entry (or empty) client copy.
`denseListEditor` gates that behind `requireLoad` + `setLoaded`; this fails the
next editor added without it.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"

# Gates the same failure itself (its Add button and rule fields stay disabled
# until the read lands), so it opts out of the shared flag.
OWN_GUARD = {"devices/units.js"}

CALL = re.compile(r"(\w+)\s*=\s*denseListEditor\(\{(.*?)\n\}\);", re.S)


def _editors():
    for path in sorted(STATIC.rglob("*.js")):
        rel = path.relative_to(STATIC).as_posix()
        if rel == "dense-editor.js":
            continue
        text = path.read_text(encoding="utf-8")
        for name, body in CALL.findall(text):
            yield rel, name, body, text


def test_editors_are_discovered() -> None:
    assert len(list(_editors())) >= 8


def test_every_dense_editor_requires_a_successful_load() -> None:
    missing = [
        f"{rel}:{name}"
        for rel, name, body, _ in _editors()
        if rel not in OWN_GUARD and "requireLoad: true" not in body
    ]
    assert not missing, f"denseListEditor without requireLoad: {missing}"


def test_every_gated_editor_reports_load_results() -> None:
    silent = [
        f"{rel}:{name}"
        for rel, name, body, text in _editors()
        if "requireLoad: true" in body
        and not (f"{name}.setLoaded(true)" in text and f"{name}.setLoaded(false)" in text)
    ]
    assert not silent, f"gated editors never told the load result: {silent}"
