"""Auth gate UI — a 401 opens the vendored sign-in sheet (``dialog.sign-in``);
the password flow swaps a password for the bearer token and loads the dashboard.

The bearer middleware bypasses loopback, so we can't provoke a real 401
from 127.0.0.1 — instead we stub the API responses at the browser, which
exercises the exact frontend wiring (api.js → showLogin → requestSignIn →
/api/login → writeToken → loadUnits)."""

from __future__ import annotations

import json
from typing import Dict, List

from playwright.sync_api import Page, Route, expect

from tests.e2e._surface import surface

SHEET = "dialog.sign-in"
FIELD = f"{SHEET} input[type=password]"
SUBMIT = f"{SHEET} .sign-in-submit"


def test_401_shows_sign_in_sheet(page: Page, base_url: str) -> None:
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=401, content_type="application/json",
            body=json.dumps({"detail": "missing or invalid bearer token"}),
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator(SHEET)).to_be_visible()
    expect(page.locator(FIELD)).to_be_editable()
    surface(page, "shared.login")


def test_password_login_loads_dashboard(
    page: Page, base_url: str, sample_units: List[Dict]
) -> None:
    state = {"authed": False}

    def units(route: Route) -> None:
        if state["authed"]:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"units": sample_units}))
        else:
            route.fulfill(status=401, content_type="application/json",
                          body=json.dumps({"detail": "missing or invalid bearer token"}))

    def login(route: Route) -> None:
        state["authed"] = True
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"token": "test-token"}))

    page.route("**/api/units", units)
    page.route("**/api/login", login)

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator(SHEET)).to_be_visible()

    page.locator(FIELD).fill("hunter2")
    page.locator(SUBMIT).click()

    # The sheet closes and the dashboard renders from the now-authed fetch.
    expect(page.locator(SHEET)).to_be_hidden()
    expect(page.locator("#acUnits .ac-row")).to_have_count(len(sample_units))
    # The token was stashed under the app's existing key for subsequent calls.
    assert page.evaluate("localStorage.getItem('home-automation.token')") == "test-token"


def test_bad_password_keeps_sheet(page: Page, base_url: str) -> None:
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=401, content_type="application/json",
            body=json.dumps({"detail": "missing or invalid bearer token"}),
        ),
    )
    page.route(
        "**/api/login",
        lambda route: route.fulfill(
            status=401, content_type="application/json",
            body=json.dumps({"detail": "bad password"}),
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.locator(FIELD).fill("wrong")
    page.locator(SUBMIT).click()
    expect(page.locator(f"{SHEET} .sign-in-error")).to_be_visible()
    expect(page.locator(f"{SHEET} .sign-in-error")).to_contain_text("Wrong password")
    expect(page.locator(SHEET)).to_be_visible()
    assert page.evaluate("localStorage.getItem('home-automation.token')") is None
    surface(page, "shared.login-error")


def test_signed_in_device_sees_no_sheet(
    page: Page, base_url: str, sample_units: List[Dict]
) -> None:
    """A device that already holds a token boots straight to the dashboard."""
    page.add_init_script(
        "localStorage.setItem('home-automation.token', 'existing-token')"
    )
    seen: List[str] = []

    def units(route: Route) -> None:
        seen.append(route.request.headers.get("authorization", ""))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"units": sample_units}))

    page.route("**/api/units", units)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#acUnits .ac-row")).to_have_count(len(sample_units))
    expect(page.locator(SHEET)).to_have_count(0)
    assert "Bearer existing-token" in seen
