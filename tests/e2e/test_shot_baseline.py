"""The screenshot baseline's capture: the main tabs at phone and desktop, light and dark.

``scripts/shot_baseline.py`` compares the PNGs this module writes under
``tests/e2e/shots`` with the committed copies, so a move-only refactor proves
"0 drifted" from the gate. Every screen renders from the same stubbed fixture
data the rest of the suite uses (obvious fake rooms, never live data), because
the baseline is committed to a public repo.

``variant_page`` builds each context itself (pinned clock, time zone, locale,
viewport), so the module overrides ``page`` with it: the ``mock_*`` fixtures
then install their route stubs on that pinned page. Chromium only — a baseline
is one rasteriser's pixels, and WebKit's iPhone projection is deselected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Iterator, List

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e._shot import Variant, shot, variant_page, variants

pytestmark = pytest.mark.chromium_only

SHOTS_DIR = Path(__file__).resolve().parent / "shots"

# The app's own persistence key (state.js THEME_KEY); the init script sets it so
# the theme is the app's explicit choice, not only the emulated media query.
_THEME_KEY = "home-automation.theme"

# Screen name -> (tab button, the element that proves its first data painted).
_SCREENS = {
    "home": (None, "#paneHome .flow-row"),
    "ac": ("#tabAc", "#acUnits .ac-row"),
    "energy": ("#tabEnergy", "#paneEnergy .flow-row"),
    "security": ("#tabSecurity", "#paneSecurity[data-state='ready']"),
}


# Reads the other `mock_*` fixtures leave to the booted app, which answers them
# from this machine's gitignored config (cameras, reminders, home location), from
# the internet (weather) or from the checkout itself (build sha). Each is stubbed so the committed shots hold only
# fixture data and read the same on every machine.
_MACHINE_STATE = {
    # The footer's "Build: <sha> · <time>" changes with every commit.
    "/api/version": {"git_sha": "fixture", "built_at": "2026-01-15T09:00:00+00:00"},
    "/api/weather": {
        "available": True, "label": "Home", "weather_code": 3, "is_day": True,
        "temperature_c": 13, "forecast_code": 3, "temp_min_c": 10, "temp_max_c": 17,
    },
    "/api/ups": {"ups": {
        "available": True, "source": "nut", "status": "online", "mains_online": True,
        "battery_charge_pct": 90, "runtime_seconds": 3600, "alarms": [],
    }},
    "/api/wake-alarms": {"entries": []},
    "/api/wake-timers": {"timers": []},
    "/api/reminders": {"entries": []},
    "/api/cameras": {"cameras": [
        {"id": "cam-1", "display_name": "Camera one", "reachable": True, "model": "Fixture camera",
         "recording": False, "ptz_presets": False, "ptz_absolute": False},
        {"id": "cam-2", "display_name": "Camera two", "reachable": True, "model": "Fixture camera",
         "recording": False, "ptz_presets": False, "ptz_absolute": False},
    ]},
    "/api/presence/automation": {
        "auto_arm_enabled": False, "arm_away_after_s": 300,
        "stale_after_s": 3600, "auto_disarm_enabled": False,
    },
}


def _json_route(body: Dict) -> Callable[[Route], None]:
    return lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(body))


def _stub_machine_state(page: Page) -> None:
    for path, body in _MACHINE_STATE.items():
        page.route(f"**{path}", _json_route(body))
    page.route("**/api/cameras/*/last_snapshot**", lambda route: route.fulfill(status=204))


@pytest.fixture(params=variants(), ids=lambda v: v.slug)
def variant(request: pytest.FixtureRequest) -> Variant:
    return request.param


@pytest.fixture
def page(browser: Browser, variant: Variant) -> Iterator[Page]:
    with variant_page(browser, variant) as pinned:
        pinned.add_init_script(
            f"try {{ localStorage.setItem({_THEME_KEY!r}, {variant.theme!r}); }} catch (e) {{}}"
        )
        yield pinned


@pytest.mark.parametrize("screen", _SCREENS)
def test_screen_shots(
    screen: str, variant: Variant, page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _stub_machine_state(page)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    tab, ready = _SCREENS[screen]
    page.wait_for_selector("#paneHome", state="visible")
    if tab:
        page.locator(tab).click()
    expect(page.locator(ready).first).to_be_visible()
    shot(page, SHOTS_DIR / f"{screen}-{variant.slug}.png")
