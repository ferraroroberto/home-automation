"""Auth gate UI — a 401 surfaces the login overlay; the password flow
swaps a password for the bearer token and loads the dashboard.

The bearer middleware bypasses loopback, so we can't provoke a real 401
from 127.0.0.1 — instead we stub the API responses at the browser, which
exercises the exact frontend wiring (api.js → showLogin, main.js login
submit → /api/login → writeToken → reload)."""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect


def test_401_shows_login_overlay(page: Page, base_url: str) -> None:
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=401, content_type="application/json",
            body=json.dumps({"detail": "missing or invalid bearer token"}),
        ),
    )
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#loginOverlay")).to_be_visible()
    expect(page.locator("#loginPassword")).to_be_editable()


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
    expect(page.locator("#loginOverlay")).to_be_visible()

    page.locator("#loginPassword").fill("hunter2")
    page.locator("#loginForm button[type=submit]").click()

    # Overlay clears and the dashboard renders from the now-authed fetch.
    expect(page.locator("#loginOverlay")).to_be_hidden()
    expect(page.locator(".unit-card")).to_have_count(len(sample_units))
    # The token was stashed for subsequent calls.
    assert page.evaluate("localStorage.getItem('home-automation.token')") == "test-token"


def test_bad_password_keeps_overlay(page: Page, base_url: str) -> None:
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
    page.locator("#loginPassword").fill("wrong")
    page.locator("#loginForm button[type=submit]").click()
    expect(page.locator("#loginError")).to_be_visible()
    expect(page.locator("#loginError")).to_contain_text("bad password")
    expect(page.locator("#loginOverlay")).to_be_visible()
