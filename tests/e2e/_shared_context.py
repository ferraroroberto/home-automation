"""One browser context per distinct context arguments per projection, reset between tests.

A fresh context per test starts with no sockets and an empty HTTP cache, so every
test pays its own connections to the app, and every one it closes parks a
loopback port in TIME_WAIT (see ``_port_pool``). Here a context outlives the
test that asked for it: each test gets a new page in it, and the reset below
puts back what a context keeps between pages.

The reset covers every piece of context state this suite touches: the pages, the
context's routes, cookies and permission grants, the app origin's local storage
and its service-worker registrations (session storage dies with the page). A
test that needs a context nothing else shares (a different viewport, touch, a
pinned clock) passes its own context arguments and so gets its own, keyed by
those arguments; one that sets context-level state this reset does not undo
(init scripts, extra headers, offline, geolocation) must build a context itself.

A reset that raises discards the context, so a damaged one never reaches the next test.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Route

logger = logging.getLogger(__name__)

# Runs on the app's origin, after every page is closed: what outlives a page there.
_CLEAR_ORIGIN_STATE = """async () => {
  localStorage.clear();
  sessionStorage.clear();
  const registrations = await navigator.serviceWorker.getRegistrations();
  await Promise.all(registrations.map((r) => r.unregister()));
}"""

#: How the shared contexts were used this session, for the run summary.
STATS = {"created": 0, "reused": 0, "discarded": 0}


def context_key(args: dict[str, Any]) -> str:
    """A stable identity for a set of context arguments."""
    return json.dumps(args, sort_keys=True, default=repr)


def _blank(route: Route) -> None:
    route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>reset</title>")


def reset_context(context: BrowserContext, origin: str) -> None:
    """Return *context* to what a freshly created one holds, for *origin*'s state.

    The app's origin is answered by a route, so clearing its storage opens no
    socket to the app.
    """
    for page in list(context.pages):
        page.close()
    context.unroute_all(behavior="ignoreErrors")
    context.clear_cookies()
    context.clear_permissions()
    scratch = context.new_page()
    try:
        scratch.route(f"{origin}/**", _blank)
        scratch.goto(f"{origin}/")
        scratch.evaluate(_CLEAR_ORIGIN_STATE)
    finally:
        scratch.close()


class SharedContexts:
    """The session's contexts on one browser, keyed by their context arguments."""

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._by_key: dict[str, BrowserContext] = {}

    def acquire(self, args: dict[str, Any]) -> BrowserContext:
        key = context_key(args)
        context = self._by_key.get(key)
        if context is None:
            context = self._browser.new_context(**args)
            self._by_key[key] = context
            STATS["created"] += 1
        else:
            STATS["reused"] += 1
        return context

    def release(self, context: BrowserContext, args: dict[str, Any], origin: str) -> None:
        """Reset *context* for the next test, or drop it when the reset fails."""
        try:
            reset_context(context, origin)
        except Exception:
            logger.warning("⚠️ shared browser context reset failed; discarding it", exc_info=True)
            STATS["discarded"] += 1
            self._by_key.pop(context_key(args), None)
            try:
                context.close()
            except Exception:
                logger.warning("⚠️ discarded context did not close cleanly", exc_info=True)


class _Lease:
    """A shared context handed out as a private one: ``close()`` resets it and gives it back."""

    def __init__(self, shared: SharedContexts, context: BrowserContext, args: dict[str, Any], origin: str) -> None:
        self._shared, self._context, self._args, self._origin = shared, context, args, origin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def close(self) -> None:
        self._shared.release(self._context, self._args, self._origin)


class LeasingBrowser:
    """Stands in for a ``Browser`` whose ``new_context`` leases from *shared*.

    For code that builds and closes its own context, like the vendored
    ``variant_page``, so it keeps its pins while the sockets of one pinned
    variant are reused by every screen captured in it.
    """

    def __init__(self, shared: SharedContexts, origin: str) -> None:
        self._shared, self._origin = shared, origin

    def new_context(self, **args: Any) -> Any:
        return _Lease(self._shared, self._shared.acquire(args), args, self._origin)


def write_summary(terminalreporter: Any) -> None:
    total = STATS["created"] + STATS["reused"]
    if total:
        terminalreporter.write_line(
            f"shared contexts: {STATS['created']} created for {total} tests "
            f"({STATS['reused']} reused, {STATS['discarded']} discarded after a failed reset)"
        )
