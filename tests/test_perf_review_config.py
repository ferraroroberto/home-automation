"""``.fleet.toml`` declares what /perf-review waits for on a cold launch (#777).

Without ``ready_selector`` "ready" falls back to first contentful paint, which
goes green on a painted shell before any card has data. That the selector shows
on Home is pinned in ``tests/e2e/test_home_tab.py``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

FLEET_TOML = Path(__file__).resolve().parent.parent / ".fleet.toml"


def test_ready_selector_is_declared() -> None:
    block = tomllib.loads(FLEET_TOML.read_text(encoding="utf-8"))
    assert block.get("perf", {}).get("review", {}).get("ready_selector", "") != ""
