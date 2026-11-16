"""The e2e suite shares one browser context per distinct arguments and resets it between tests.

A context that outlives its test is only safe if what a test leaves in it is
undone, and if no test leaves what the reset cannot undo. Pinned here against
recording stand-ins: the reset's contents and order, reuse keyed by context
arguments, a failed reset discarding the context, and a static check that every
test installing the page clock (context-level state) asks for a context of its own.
No browser is started.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from tests.e2e._shared_context import STATS, SharedContexts, context_key, reset_context

_E2E = Path(__file__).resolve().parents[1] / "e2e"


class _Page:
    def __init__(self, log: List[str], label: str) -> None:
        self._log, self._label = log, label

    def close(self) -> None:
        self._log.append(f"close {self._label}")

    def route(self, pattern: str, handler: Any) -> None:
        self._log.append(f"route {pattern}")

    def goto(self, url: str) -> None:
        self._log.append(f"goto {url}")

    def evaluate(self, script: str) -> None:
        self._log.append("clear origin state")


class _Context:
    def __init__(self, fail_on: str = "") -> None:
        self.log: List[str] = []
        self.closed = False
        self._fail_on = fail_on
        self.pages = [_Page(self.log, "page-1"), _Page(self.log, "page-2")]

    def _step(self, name: str) -> None:
        self.log.append(name)
        if name == self._fail_on:
            raise RuntimeError(name)

    def unroute_all(self, behavior: str) -> None:
        self._step("unroute_all")

    def clear_cookies(self) -> None:
        self._step("clear_cookies")

    def clear_permissions(self) -> None:
        self._step("clear_permissions")

    def new_page(self) -> _Page:
        return _Page(self.log, "scratch")

    def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self) -> None:
        self.made: List[_Context] = []

    def new_context(self, **args: Any) -> _Context:
        context = _Context()
        self.made.append(context)
        return context


def test_reset_closes_pages_then_clears_what_a_context_keeps_without_a_socket_to_the_app() -> None:
    context = _Context()

    reset_context(context, "http://app.test")  # type: ignore[arg-type]

    assert context.log == [
        "close page-1", "close page-2", "unroute_all", "clear_cookies", "clear_permissions",
        "route http://app.test/**", "goto http://app.test/", "clear origin state", "close scratch",
    ]


def test_the_same_arguments_reuse_one_context_and_different_arguments_get_another() -> None:
    browser = _Browser()
    shared = SharedContexts(browser)  # type: ignore[arg-type]
    phone: Dict[str, Any] = {"viewport": {"width": 390, "height": 844}, "is_mobile": True}
    desktop: Dict[str, Any] = {"viewport": {"width": 1280, "height": 720}}

    first = shared.acquire(phone)
    assert shared.acquire(dict(reversed(list(phone.items())))) is first
    assert shared.acquire(desktop) is not first
    assert len(browser.made) == 2


def test_context_key_ignores_argument_order() -> None:
    assert context_key({"a": 1, "b": 2}) == context_key({"b": 2, "a": 1})
    assert context_key({"a": 1}) != context_key({"a": 2})


def test_a_failed_reset_discards_the_context_so_the_next_test_gets_a_fresh_one() -> None:
    browser = _Browser()
    shared = SharedContexts(browser)  # type: ignore[arg-type]
    args: Dict[str, Any] = {"locale": "en-GB"}
    damaged = shared.acquire(args)
    assert isinstance(damaged, _Context)
    damaged._fail_on = "clear_cookies"
    discarded_before = STATS["discarded"]

    shared.release(damaged, args, "http://app.test")

    assert damaged.closed
    assert STATS["discarded"] == discarded_before + 1
    assert shared.acquire(args) is not damaged


def test_every_test_that_installs_the_page_clock_asks_for_a_context_of_its_own() -> None:
    """`page.clock` is context-level: a clock a test installs would otherwise run in the next test."""
    offenders = [
        path.name
        for path in sorted(_E2E.rglob("test_*.py"))
        if re.search(r"\.clock\.(install|set_fixed_time)\(", path.read_text(encoding="utf-8"))
        and "pytest.mark.own_context" not in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"{offenders} install the page clock without @pytest.mark.own_context"
