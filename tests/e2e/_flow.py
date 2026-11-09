"""One browser test per user-visible flow, each flow a sequence of scenario steps.

The fleet budget is fewer than 15 browser tests per repo, and every test pays a
fresh browser context and page. A *flow* test runs several scenarios — the
"steps", plain functions that take fixture names as parameters exactly as a test
would — one after another on a single page, so a regression in any step fails
the flow and its message names the step.

Between steps the page is put back to what a new test would see: blank page,
this origin's storage and the cookies cleared, every route and init script this
flow added removed, the always-on stubs reinstalled, the starting viewport and
media emulation restored. Data fixtures (``sample_*``) are handed to each step
as a fresh copy; the stub factories (``mock_*``) are stateless until called, so
each step builds its own stub state.

What a step cannot undo is page-level: ``page.clock.install()`` stays installed
for the page's life. A scenario that installs the clock stays a test of its own.
"""

from __future__ import annotations

import copy
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pytest
from playwright.sync_api import Page

from tests.e2e.fixtures.stubs import install_baseline_stubs

Step = Union[Callable[..., None], Tuple[Callable[..., None], Dict[str, Any]]]

#: Names a step receives from the flow itself rather than from a fixture.
_FLOW_ARGS = ("page", "base_url")


def _fresh(value: Any) -> Any:
    """A private copy of a fixture value; callables and handles are shared."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _is_chromium_only(step: Callable[..., None]) -> bool:
    marks = getattr(step, "pytestmark", [])
    return any(mark.name == "chromium_only" for mark in marks)


def _reset_page(
    page: Page, base_url: str, viewport: Dict[str, int], init_scripts: List[Any],
) -> None:
    """Return ``page`` to the state a fresh test starts from."""
    if page.url.startswith(base_url):
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    page.goto("about:blank")
    page.unroute_all(behavior="ignoreErrors")
    while init_scripts:
        init_scripts.pop().dispose()
    page.context.clear_cookies()
    page.set_viewport_size(viewport)  # type: ignore[arg-type]
    page.emulate_media(color_scheme="null")  # type: ignore[arg-type]
    install_baseline_stubs(page)


def run_flow(
    request: pytest.FixtureRequest, page: Page, base_url: str, *steps: Step,
    per_step: Optional[Callable[[], None]] = None,
) -> None:
    """Run ``steps`` in order on ``page``; a failure names the step it came from.

    A step is a function, or a ``(function, {param: value})`` pair for a step
    that takes an extra argument. A step marked ``chromium_only`` is skipped on
    the WebKit projection, as the same test would be deselected there.
    ``per_step`` runs before every step, for the routes a module-level fixture
    used to install once per test (the reset removes them).
    """
    browser_name = request.getfixturevalue("browser_name")
    viewport = page.viewport_size or {"width": 1280, "height": 720}

    init_scripts: List[Any] = []
    add_init_script = page.add_init_script

    def tracked_add_init_script(*args: Any, **kwargs: Any) -> Any:
        handle = add_init_script(*args, **kwargs)
        init_scripts.append(handle)
        return handle

    page.add_init_script = tracked_add_init_script  # type: ignore[method-assign]

    resolved = [(s[0], dict(s[1])) if isinstance(s, tuple) else (s, {}) for s in steps]
    needed = {
        name
        for func, extra in resolved
        for name in inspect.signature(func).parameters
        if name not in _FLOW_ARGS and name not in extra
    }
    pristine = {name: _fresh(request.getfixturevalue(name)) for name in sorted(needed)}

    ran = 0
    for func, extra in resolved:
        if browser_name == "webkit" and _is_chromium_only(func):
            continue
        if ran:
            _reset_page(page, base_url, viewport, init_scripts)
        if per_step is not None:
            per_step()
        kwargs: Dict[str, Any] = {}
        for name in inspect.signature(func).parameters:
            if name == "page":
                kwargs[name] = page
            elif name == "base_url":
                kwargs[name] = base_url
            elif name in extra:
                kwargs[name] = extra[name]
            else:
                kwargs[name] = _fresh(pristine[name])
        try:
            func(**kwargs)
        except BaseException as exc:
            exc.add_note(f"flow step: {func.__name__}")
            raise
        ran += 1
