"""The e2e flow runner puts the page back between steps and names a failing step.

A flow test runs several scenarios on one page, so each step must start from
what a new test would see — or a leaked route, init script or storage value makes
a later step pass or fail for the wrong reason. Pinned here against a recording
stand-in for the page: reset order, fresh data per step, the WebKit skip of a
``chromium_only`` step, the failing step's name on the error, and the per-step
hook. No browser is started.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from tests.e2e._flow import run_flow


class _Handle:
    def __init__(self, log: List[str], label: str) -> None:
        self._log, self._label = log, label

    def close(self) -> None:
        self._log.append(f"close {self._label}")


class _Context:
    def __init__(self, log: List[str]) -> None:
        self._log = log

    def clear_cookies(self) -> None:
        self._log.append("clear_cookies")


class _FakePage:
    url = "http://app.test/"
    viewport_size = {"width": 390, "height": 844}

    def __init__(self) -> None:
        self.log: List[str] = []
        self.context = _Context(self.log)

    def evaluate(self, script: str) -> None:
        self.log.append("clear_storage")

    def goto(self, url: str) -> None:
        self.log.append(f"goto {url}")

    def unroute_all(self, behavior: str) -> None:
        self.log.append("unroute_all")

    def route(self, pattern: str, handler: Any) -> None:
        self.log.append(f"route {pattern}")

    def set_viewport_size(self, size: Dict[str, int]) -> None:
        self.log.append(f"viewport {size['width']}")

    def emulate_media(self, color_scheme: str) -> None:
        self.log.append(f"media {color_scheme}")

    def add_init_script(self, script: str) -> _Handle:
        self.log.append(f"init {script}")
        return _Handle(self.log, script)


class _FakeRequest:
    def __init__(self, fixtures: Dict[str, Any]) -> None:
        self._fixtures = fixtures

    def getfixturevalue(self, name: str) -> Any:
        return self._fixtures[name]


def _run(*steps: Any, browser: str = "chromium", **kwargs: Any) -> _FakePage:
    page = _FakePage()
    request = _FakeRequest({"browser_name": browser, "units": [{"n": 1}]})
    run_flow(request, page, "http://app.test", *steps, **kwargs)  # type: ignore[arg-type]
    return page


def test_page_is_reset_between_steps_and_not_before_the_first() -> None:
    seen: List[str] = []

    def first(page: _FakePage) -> None:
        page.add_init_script("hold")
        seen.append("first")

    def second(page: _FakePage) -> None:
        page.log.append("second runs")
        seen.append("second")

    page = _run(first, second)

    assert seen == ["first", "second"]
    reset = page.log[page.log.index("init hold") + 1 : page.log.index("second runs")]
    assert reset[:3] == ["clear_storage", "goto about:blank", "unroute_all"]
    assert "close hold" in reset
    assert "clear_cookies" in reset
    assert "viewport 390" in reset and "media null" in reset
    assert any(entry.startswith("route ") for entry in reset), "baseline stubs are reinstalled"
    assert "clear_storage" not in page.log[: page.log.index("init hold")]


def test_each_step_gets_its_own_copy_of_a_data_fixture() -> None:
    received: List[List[Dict[str, int]]] = []

    def step(page: _FakePage, units: List[Dict[str, int]]) -> None:
        received.append(units)
        units[0]["n"] += 10

    _run(step, step)

    assert received[0] == received[1] == [{"n": 11}]
    assert received[0] is not received[1]


def test_extra_arguments_are_passed_and_need_no_fixture() -> None:
    got: List[str] = []

    def step(page: _FakePage, theme: str) -> None:
        got.append(theme)

    _run((step, {"theme": "light"}), (step, {"theme": "dark"}))

    assert got == ["light", "dark"]


def test_a_chromium_only_step_is_skipped_on_webkit_only() -> None:
    ran: List[str] = []

    def dom_only(page: _FakePage) -> None:
        ran.append("dom_only")

    # The mark's shape without registering it in this suite's marker config.
    dom_only.pytestmark = [SimpleNamespace(name="chromium_only")]  # type: ignore[attr-defined]

    def layout(page: _FakePage) -> None:
        ran.append("layout")

    _run(dom_only, layout, browser="webkit")
    assert ran == ["layout"]

    ran.clear()
    _run(dom_only, layout, browser="chromium")
    assert ran == ["dom_only", "layout"]


def test_a_failing_step_is_named_and_stops_the_flow() -> None:
    ran: List[str] = []

    def passes(page: _FakePage) -> None:
        ran.append("passes")

    def breaks(page: _FakePage) -> None:
        raise AssertionError("the sheet never opened")

    def never(page: _FakePage) -> None:
        ran.append("never")

    with pytest.raises(AssertionError) as caught:
        _run(passes, breaks, never)

    assert ran == ["passes"]
    assert "flow step: breaks" in caught.value.__notes__


def test_per_step_hook_runs_before_every_step() -> None:
    order: List[str] = []

    def step(page: _FakePage) -> None:
        order.append("step")

    _run(step, step, per_step=lambda: order.append("hook"))

    assert order == ["hook", "step", "hook", "step"]
