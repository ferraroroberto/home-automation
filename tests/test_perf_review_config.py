"""``.fleet.toml`` declares what /perf-review waits for on a cold launch (#777).

Without ``ready_selector`` "ready" falls back to first contentful paint, which
goes green on a painted shell before any card has data. The selector must name
markup the app really renders, or the review would score nothing.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "webapp" / "static"


def _ready_selector() -> str:
    block = tomllib.loads((ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    return block.get("perf", {}).get("review", {}).get("ready_selector", "")


def test_ready_selector_is_declared() -> None:
    assert _ready_selector() != ""


def test_ready_selector_names_markup_the_app_renders() -> None:
    parent_id, card_class = re.fullmatch(r"#([\w-]+) \.([\w-]+)", _ready_selector()).groups()
    assert f'id="{parent_id}"' in (STATIC / "index.html").read_text(encoding="utf-8")
    assert card_class in (STATIC / "units.js").read_text(encoding="utf-8")
