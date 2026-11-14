"""The shell never reloads itself on a build change.

A reload the page triggers on its own can drop unsent input; the only reload a
build change may cause is one the owner taps for.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = PROJECT_ROOT / "app" / "webapp" / "static"
_RELOAD = re.compile(r"location\s*\.\s*reload\s*\(")


def test_no_page_script_reloads_the_page_by_itself() -> None:
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for pattern in ("*.js", "*.html")
        for path in STATIC_DIR.rglob(pattern)
        if "_vendored" not in path.relative_to(STATIC_DIR).parts
        and _RELOAD.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
