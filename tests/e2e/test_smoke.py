"""Smoke tests — the dashboard boots and renders the unit rows.

Tight by design: catches JS exceptions on boot, a broken render, and the
core row anatomy (power switch, avatar badge, room → set line). Expand
only when a real regression slips through.

The build readout's restart sheet rides here too: the restart itself is always
stubbed (``/api/restart`` and ``/api/version`` are routed), so no run ever
restarts the app under test.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._flow import run_flow


def _boot(page: Page, base_url: str) -> list:
    errors: list = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    return errors


def _boots_without_console_errors(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    errors = _boot(page, base_url)
    page.wait_for_selector("#acUnits .ac-row", state="attached")
    page.wait_for_timeout(300)
    assert errors == [], "JS errors during boot:\n  - " + "\n  - ".join(errors)


def _renders_unit_rows_with_their_anatomy(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _boot(page, base_url)
    rows = page.locator("#acUnits .ac-row")
    expect(rows).to_have_count(len(sample_units))
    # Names from the fixtures land in the row titles.
    for u in sample_units:
        row = page.locator(f'#acUnits [data-unit-id="{u["unit_id"]}"]')
        expect(row.locator(".action-row-title")).to_have_text(u["name"])

    row = page.locator('#acUnits [data-unit-id="unit-1"]')
    # Power switch reflects power=True, and the running unit's avatar is badged.
    expect(row.locator(".ac-line-toggle")).to_have_attribute("aria-checked", "true")
    expect(row.locator(".row-avatar")).to_have_attribute("data-badge", "up")
    # One meta line: room → set.
    expect(row.locator(".action-row-meta")).to_contain_text("22.5")
    expect(row.locator(".action-row-meta")).to_contain_text("24.0")

    # An off unit: switch off, no badge.
    off = page.locator('#acUnits [data-unit-id="unit-2"]')
    expect(off.locator(".ac-line-toggle")).to_have_attribute("aria-checked", "false")
    expect(off.locator(".row-avatar")).not_to_have_attribute("data-badge", "up")


def _stub_restart(page: Page, *, record: dict) -> list:
    """Route the restart endpoints; return the POST bodies the page sent.

    ``/api/version`` reports one process until the POST lands, then a new one,
    exactly what the sheet waits for. ``record`` is the server's run record,
    given its id once the POST has been answered.
    """
    posts: list = []

    def version(route) -> None:
        built = "2026-02-02T10:00:00+00:00" if posts else "2026-01-15T09:00:00+00:00"
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"git_sha": "abc1234", "built_at": built}),
        )

    def restart(route) -> None:
        if route.request.method == "POST":
            posts.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=202, content_type="application/json",
                body=json.dumps({"restart_id": "r1", "target": "app", "started_at": "now"}),
            )
            return
        last = dict(record, id="r1", target="app") if posts else None
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "targets": [{"id": "app", "label": "Restart app", "warning": ""}],
                "last": last,
            }),
        )

    page.route("**/api/version", version)
    page.route("**/api/restart", restart)
    return posts


@pytest.mark.chromium_only
def _build_readout_restarts_the_app_and_reloads_on_the_new_build(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    posts = _stub_restart(page, record={"state": "ok", "exit_code": 0})
    _boot(page, base_url)

    readout = page.locator("#buildReadout")
    expect(readout).to_have_attribute("role", "button")
    readout.click()
    sheet = page.locator('dialog[data-surface="app.restart-sheet"]')
    expect(sheet).to_be_visible()
    page.evaluate("window.__beforeRestart = true")
    sheet.get_by_role("button", name="Restart app").click()

    # A new process plus an `ok` record reloads the page onto the new build.
    page.wait_for_function("window.__beforeRestart === undefined", timeout=15_000)
    assert posts == [{"target": "app"}]
    expect(page.locator("#buildReadout")).to_contain_text("abc1234")


@pytest.mark.chromium_only
def _failed_restart_is_shown_with_its_reason_and_never_as_success(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable
) -> None:
    mock_api(sample_units)
    _stub_restart(page, record={"state": "failed", "exit_code": 7, "log_tail": ["recipe broke"]})
    _boot(page, base_url)

    page.locator("#buildReadout").click()
    sheet = page.locator('dialog[data-surface="app.restart-sheet"]')
    sheet.get_by_role("button", name="Restart app").click()

    expect(sheet.locator(".restart-sheet-note")).to_contain_text("Restart failed (exit 7)", timeout=15_000)
    expect(sheet.locator(".restart-sheet-log")).to_contain_text("recipe broke")
    expect(sheet.get_by_role("button", name="Try again")).to_be_enabled()


def test_app_boots_and_renders_unit_rows(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """The dashboard boots without JS errors and renders each unit row with its anatomy."""
    run_flow(
        request, page, base_url,
        _boots_without_console_errors,
        _renders_unit_rows_with_their_anatomy,
        _build_readout_restarts_the_app_and_reloads_on_the_new_build,
        _failed_restart_is_shown_with_its_reason_and_never_as_success,
    )
