"""PC-fleet UPS-shutdown card (IoT tab, issue #498).

Drives the PC-fleet card against Playwright route stubs for ``/api/pc-fleet/*``
— never the real hub. The card owns the fleet's desired-state shutdown prefs
(master enable + runtime-remaining threshold + an ``excluded`` id list) and a
live machine roster read from the hub. These tests cover: the machine list with
per-state chips, the include-toggle → prefs PUT (``excluded`` semantics), the
master toggle + threshold PUT, the Wake button visibility rule + its no-confirm
POST, and the hub-unreachable note that keeps prefs editable.

Machine ids/names are obvious fixtures, never real hosts (the repo is public).
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, Route, expect


def _sample_machines() -> List[Dict]:
    """Four fake machines covering each render branch: the hub host (always
    last, no toggle), an up peer, a down + wake-capable peer, and a dormant
    peer with no wake capability."""
    return [
        {"id": "hub-host", "display_name": "Fixture Hub", "state": "self",
         "is_host": True, "actions": {"wake": False}},
        {"id": "workstation", "display_name": "Fixture Workstation", "state": "up",
         "is_host": False, "actions": {"wake": True}},
        {"id": "media-pc", "display_name": "Fixture Media PC", "state": "down",
         "is_host": False, "actions": {"wake": True}},
        {"id": "old-laptop", "display_name": "Fixture Laptop", "state": "dormant",
         "is_host": False, "actions": {"wake": False}},
    ]


def _stub_pc_fleet(
    page: Page,
    prefs: Dict,
    machines: List[Dict],
    hub_down: bool = False,
) -> Dict:
    """Install route stubs for ``/api/pc-fleet/*``.

    Returns a mutable observability dict: ``prefs`` (the live prefs store, which
    PUT mutates), ``put_bodies`` (every PUT payload), and ``wake_ids`` (every
    POST /wake/{id}). Call before navigating.
    """
    store: Dict = {
        "prefs": dict(prefs),
        "put_bodies": [],
        "wake_ids": [],
    }

    def handle(route: Route) -> None:
        req = route.request
        url = req.url
        method = req.method.upper()

        if "/api/pc-fleet/machines" in url:
            if hub_down:
                route.fulfill(status=502, content_type="application/json",
                              body=json.dumps({"detail": "hub unreachable"}))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"machines": machines}))
            return

        if "/api/pc-fleet/wake/" in url and method == "POST":
            mid = url.split("/api/pc-fleet/wake/", 1)[1].rstrip("/")
            store["wake_ids"].append(mid)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "sent": True}))
            return

        if "/api/pc-fleet/prefs" in url:
            if method == "PUT":
                body = req.post_data_json or {}
                store["put_bodies"].append(body)
                store["prefs"] = dict(body)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(store["prefs"]))
            return

        route.fulfill(status=404, content_type="application/json",
                      body=json.dumps({"detail": "not found"}))

    page.route("**/api/pc-fleet/**", handle)
    return store


def _boot_pc_fleet(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    prefs: Dict, machines: List[Dict], hub_down: bool = False,
) -> Dict:
    """Stub every endpoint the boot + IoT tab touches (units/energy/tuya/ups/
    notify-prefs never reach the cloud), install the pc-fleet stubs, open the
    IoT tab, and expand the (collapsed-by-default) PC-fleet card."""
    mock_api(sample_units)
    mock_energy()
    mock_tuya([])
    # UPS + notify-prefs also fire on the IoT tab — keep them off the real
    # backend and deterministic.
    page.route("**/api/ups", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"ups": {"available": False, "source": "none", "error": None}})))
    page.route("**/api/ups/notify-prefs", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"prefs": {"power_lost": True, "power_restored": True},
                         "telegram_configured": True})))
    store = _stub_pc_fleet(page, prefs, machines, hub_down=hub_down)

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    page.eval_on_selector("details.pc-fleet-card", "e => { e.open = true; }")
    return store


def test_pc_fleet_renders_machine_list_with_state_chips(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    _boot_pc_fleet(page, base_url, sample_units, mock_api, mock_energy, mock_tuya,
                   {"enabled": False, "threshold_minutes": 15, "excluded": []},
                   _sample_machines())

    rows = page.locator(".pc-fleet-machine")
    expect(rows).to_have_count(4)
    # Per-machine state chips render with the state text.
    expect(page.locator('[data-machine-id="hub-host"] .pc-fleet-chip')).to_have_text("this host")
    expect(page.locator('[data-machine-id="workstation"] .pc-fleet-chip')).to_have_text("up")
    expect(page.locator('[data-machine-id="media-pc"] .pc-fleet-chip')).to_have_text("down")
    expect(page.locator('[data-machine-id="old-laptop"] .pc-fleet-chip')).to_have_text("dormant")
    # The hub host has no include toggle — it always participates, shut last.
    expect(page.locator('[data-machine-id="hub-host"] .toggle')).to_have_count(0)
    expect(page.locator('[data-machine-id="hub-host"] .pc-fleet-host-note')).to_be_visible()
    # A non-host peer carries an include toggle.
    expect(page.locator('[data-machine-id="workstation"] .toggle')).to_have_count(1)


def test_include_toggle_flip_puts_excluded(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    store = _boot_pc_fleet(page, base_url, sample_units, mock_api, mock_energy, mock_tuya,
                           {"enabled": True, "threshold_minutes": 15, "excluded": []},
                           _sample_machines())

    # Workstation starts included (toggle ON). Flipping it OFF adds its id to
    # `excluded` and PUTs the whole prefs object.
    toggle = page.locator('[data-machine-id="workstation"] .toggle')
    expect(toggle).to_have_attribute("aria-checked", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    expect(page.locator("#toast")).to_have_text("Fleet shutdown saved")

    assert store["put_bodies"], "include-toggle flip did not PUT prefs"
    last = store["put_bodies"][-1]
    assert last["excluded"] == ["workstation"], last
    assert last["enabled"] is True
    assert last["threshold_minutes"] == 15


def test_master_toggle_and_threshold_put(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    store = _boot_pc_fleet(page, base_url, sample_units, mock_api, mock_energy, mock_tuya,
                           {"enabled": False, "threshold_minutes": 15, "excluded": []},
                           _sample_machines())

    # Master toggle: OFF → ON PUTs enabled=true.
    master = page.locator("#pcFleetEnabled")
    expect(master).to_have_attribute("aria-checked", "false")
    master.click()
    expect(master).to_have_attribute("aria-checked", "true")
    # Wait for the save round-trip to settle (its read-back re-renders the card)
    # before the next edit, so the response can't clobber the mid-edit value.
    expect(page.locator("#toast")).to_have_text("Fleet shutdown saved")
    assert store["put_bodies"][-1]["enabled"] is True

    # Threshold: change commits on blur (Enter) and PUTs the new value.
    threshold = page.locator("#pcFleetThreshold")
    threshold.fill("30")
    threshold.press("Enter")
    expect(page.locator("#pcFleetCaption")).to_contain_text("≤ 30 min")
    last = store["put_bodies"][-1]
    assert last["threshold_minutes"] == 30, last
    assert last["enabled"] is True


def test_wake_button_visibility_and_no_confirm_post(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    store = _boot_pc_fleet(page, base_url, sample_units, mock_api, mock_energy, mock_tuya,
                           {"enabled": True, "threshold_minutes": 15, "excluded": []},
                           _sample_machines())

    # Wake only on the down + wake-capable machine — not self/up, not the
    # dormant machine whose actions.wake is false.
    expect(page.locator('[data-machine-id="media-pc"] .pc-fleet-wake')).to_have_count(1)
    expect(page.locator('[data-machine-id="hub-host"] .pc-fleet-wake')).to_have_count(0)
    expect(page.locator('[data-machine-id="workstation"] .pc-fleet-wake')).to_have_count(0)
    expect(page.locator('[data-machine-id="old-laptop"] .pc-fleet-wake')).to_have_count(0)

    # Clicking Wake POSTs /wake/{id} with no confirm dialog blocking it.
    dialogs: List[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))
    page.locator('[data-machine-id="media-pc"] .pc-fleet-wake').click()
    expect(page.locator("#toast")).to_have_text("Wake signal sent")
    assert store["wake_ids"] == ["media-pc"], store["wake_ids"]
    assert dialogs == [], f"unexpected confirm dialog: {dialogs}"


def test_hub_unreachable_shows_note_and_keeps_prefs_editable(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
) -> None:
    store = _boot_pc_fleet(page, base_url, sample_units, mock_api, mock_energy, mock_tuya,
                           {"enabled": False, "threshold_minutes": 15, "excluded": ["ghost-pc"]},
                           _sample_machines(), hub_down=True)

    # Machines 502 → the note shows, no machine rows render.
    expect(page.locator("#pcFleetNote")).to_be_visible()
    expect(page.locator("#pcFleetNote")).to_contain_text("not reachable")
    expect(page.locator(".pc-fleet-machine")).to_have_count(0)

    # Prefs stay editable, and a save preserves the previously-known `excluded`
    # (it is not wiped just because the roster is unavailable).
    page.locator("#pcFleetEnabled").click()
    expect(page.locator("#pcFleetEnabled")).to_have_attribute("aria-checked", "true")
    last = store["put_bodies"][-1]
    assert last["enabled"] is True
    assert last["excluded"] == ["ghost-pc"], last
