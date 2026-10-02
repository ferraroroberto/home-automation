"""Shared browser-suite helpers for booting the PWA.

Kept out of `conftest.py` because they are plain functions, not fixtures: the
per-feature `test_*.py` modules that were split out of the old monolithic
`test_tabs.py` (home-automation#634) all open the app the same way, and one
copy is better than eight.
"""

from __future__ import annotations

import json
from typing import Callable

from playwright.sync_api import Page

# Holds every browser fetch of one endpoint until the test releases it. The
# request itself still goes through Playwright's routing (and its stubs) once
# released, so the held read returns exactly what the test stubbed.
_HOLD_READS_SCRIPT = """
(() => {
  const heldUrl = %s;
  const originalFetch = window.fetch.bind(window);
  let release;
  const released = new Promise(function (resolve) { release = resolve; });
  window.__e2eReleaseHeldReads = function () { release(); };
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    if (url === heldUrl || url.endsWith(heldUrl)) {
      return released.then(function () { return originalFetch(input, init); });
    }
    return originalFetch(input, init);
  };
})();
"""


def boot_home(page: Page, base_url: str) -> None:
    """Open the app and wait for the Home pane to be painted."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


def hold_reads(page: Page, endpoint: str) -> Callable[[], None]:
    """Hold the page's reads of ``endpoint`` until the returned ``release()``.

    Makes a panel's loading state observable without a timer: assert the
    loading state, call ``release()``, then assert what the read produced.
    It replaces a fixed 750 ms delay that every loading test slept through,
    and that a loaded box could outrun (home-automation#778). Call it before
    the page loads, since it installs an init script.
    """
    page.add_init_script(_HOLD_READS_SCRIPT % json.dumps(endpoint))
    return lambda: page.evaluate("window.__e2eReleaseHeldReads()")
