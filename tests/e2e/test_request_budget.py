"""The dashboard keeps its request budget: a visible minute, a hidden minute, one catch-up read.

Every API path is answered by a catch-all route, so the page's polls run at
their real cadence on a fake clock and nothing reaches the alarm panel, a
cloud or a device. The vendored probe (``tests/e2e/_request_budget.py``)
counts the requests by path; ``.fleet.toml [request_budget]`` holds the
visible budget.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, Route

from tests.e2e._request_budget import (
    assert_request_budget,
    measure_request_budget,
    visible_budget,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Not a poll: the vendored usage log flushes the view time it holds once when
# the page is hidden, which is the moment a phone may discard the page.
_HIDE_FLUSH = "/api/usage/events"


def _answer_every_api(page: Page) -> None:
    def answer(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", answer)


def test_home_keeps_the_request_budget(page: Page, base_url: str) -> None:
    _answer_every_api(page)
    reading = measure_request_budget(page, f"{base_url}/")
    assert reading.hidden.pop(_HIDE_FLUSH, 0) <= 1, reading.summary()
    assert_request_budget(reading, visible_budget(REPO_ROOT))
    assert reading.catch_up, f"the return to the foreground reads once per poll: {reading.summary()}"
