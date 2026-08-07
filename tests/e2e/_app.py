"""Shared browser-suite helpers for booting the PWA.

Kept out of `conftest.py` because it is a plain function, not a fixture: the
per-feature `test_*.py` modules that were split out of the old monolithic
`test_tabs.py` (home-automation#634) all open the app the same way, and one
copy is better than eight.
"""

from __future__ import annotations

from playwright.sync_api import Page


def boot_home(page: Page, base_url: str) -> None:
    """Open the app and wait for the Home pane to be painted."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
