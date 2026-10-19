"""The three staged name dialogs on the shared detailModal() shell (#912).

Camera, presence entity and Elgato light each edit a display name behind a
Save. They used to wire the sheet shell by hand; these pin what the shared
shell must keep doing for each: Save disabled until an edit, Esc and the
backdrop discard the draft without a request, Save sends once, and focus goes
back to the row (or the sheet button) that opened the dialog.

DOM-state and focus behaviour only, so `chromium_only`. Every request is
stubbed: no rename ever reaches a real camera, light or presence entry.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e._app import boot_home
from tests.e2e._surface import surface

_CAMERA = {
    "id": "front-door",
    "display_name": None,
    "reachable": True,
    "model": "Fixture camera",
    "recording": False,
    "ptz_presets": False,
    "ptz_absolute": False,
}


def _record_writes(page: Page, *fragments: str) -> List[Dict]:
    """Every PUT whose URL carries one of ``fragments``, as {url, body}."""
    writes: List[Dict] = []

    def on_request(request) -> None:
        if request.method == "PUT" and any(f in request.url for f in fragments):
            writes.append({"url": request.url, "body": request.post_data_json})

    page.on("request", on_request)
    return writes


def _json_ok(route: Route) -> None:
    route.fulfill(status=200, content_type="application/json", body="{}")


def _assert_staged_name_flow(
    page: Page,
    *,
    reopen: Callable[[], None],
    dialog_id: str,
    name_input: str,
    save: str,
    writes: List[Dict],
    focus_target: Callable[[], "object"],
    row_title: "object",
    saved_name: str,
) -> None:
    dialog = page.locator(dialog_id)
    expect(dialog).to_have_attribute("data-save-model", "staged")
    expect(dialog).to_be_visible()
    expect(page.locator(save)).to_be_disabled()

    # Esc discards the draft: no request, focus back, a reopen starts clean.
    page.locator(name_input).fill("Draft name")
    expect(page.locator(save)).to_be_enabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(focus_target()).to_be_focused()
    assert writes == []
    reopen()
    expect(dialog).to_be_visible()
    expect(page.locator(name_input)).to_have_value("")
    expect(page.locator(save)).to_be_disabled()

    # The backdrop discards too.
    page.locator(name_input).fill("Another draft")
    page.mouse.click(4, 4)
    expect(dialog).to_be_hidden()
    assert writes == []
    reopen()
    expect(dialog).to_be_visible()

    # Save sends once and disarms itself.
    page.locator(name_input).fill(saved_name)
    page.locator(save).click()
    expect(page.locator("#toast")).to_contain_text("Saved")
    expect(page.locator(save)).to_be_disabled()
    expect(row_title).to_have_text(saved_name)
    assert len(writes) >= 1
    # The × closes it, and focus lands on the (re-rendered) opener.
    page.locator(dialog_id + " .detail-close").click()
    expect(dialog).to_be_hidden()
    expect(focus_target()).to_be_focused()


@pytest.mark.chromium_only
def test_camera_name_dialog_stages_the_edit(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    cameras = [dict(_CAMERA)]
    page.route("**/api/cameras/*/**", lambda route: route.fulfill(status=204))
    page.route("**/api/cameras/front-door/display_name", _json_ok)
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"cameras": cameras}),
        ),
    )
    writes = _record_writes(page, "/display_name")
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    row = page.locator('.camera-row[data-camera-id="front-door"]')
    opener = row.locator(".action-row-main")

    def reopen() -> None:
        opener.click()

    reopen()
    expect(page.locator("#cameraDialog")).to_be_visible()
    surface(page, "security.camera-dialog")
    _assert_staged_name_flow(
        page,
        reopen=reopen,
        dialog_id="#cameraDialog", name_input="#cameraDisplayName", save="#cameraSave",
        writes=writes,
        focus_target=lambda: row.locator(".action-row-main"),
        row_title=row.locator(".action-row-title"),
        saved_name="Porch",
    )
    assert len(writes) == 1
    assert writes[0]["url"].endswith("/api/cameras/front-door/display_name")
    assert writes[0]["body"] == {"display_name": "Porch"}


@pytest.mark.chromium_only
def test_presence_name_dialog_stages_the_edit(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route("**/api/presence/entity-display-name", _json_ok)
    page.route("**/api/presence/role", _json_ok)
    writes = _record_writes(page, "/api/presence/entity-display-name", "/api/presence/role")
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    row = page.locator('.presence-row[data-entity-id="home-phone"]')
    opener = row.locator(".action-row-main")

    def reopen() -> None:
        opener.click()

    reopen()
    _assert_staged_name_flow(
        page,
        reopen=reopen,
        dialog_id="#presenceDialog", name_input="#presenceDisplayName",
        save="#presenceDetailSave",
        writes=writes,
        focus_target=lambda: row.locator(".action-row-main"),
        row_title=row.locator(".action-row-title"),
        saved_name="Dad's phone",
    )
    # One Save is exactly the two writes it always made: the name and the role.
    bodies = sorted(json.dumps(w["body"], sort_keys=True) for w in writes)
    assert bodies == sorted([
        json.dumps({"entity_id": "home-phone", "display_name": "Dad's phone"}, sort_keys=True),
        json.dumps({"entity_id": "home-phone", "role": ""}, sort_keys=True),
    ])


@pytest.mark.chromium_only
def test_light_name_dialog_stages_the_edit(
    page: Page, base_url: str, sample_units: List[Dict], sample_lights: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_lights: Callable,
    mock_tuya: Callable,
) -> None:
    mock_tuya([])
    mock_api(sample_units)
    mock_energy()
    mock_lights(sample_lights)
    writes = _record_writes(page, "/display_name")
    boot_home(page, base_url)
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")

    row = page.locator('[data-light-id="192.0.2.10:9123"]')
    edit = page.locator("#lightSheetEdit")

    def reopen() -> None:
        # The light sheet is still open beneath the dialog; its Edit button
        # is both the opener and where focus goes back to.
        edit.click()

    row.locator(".action-row-main").click()
    reopen()
    _assert_staged_name_flow(
        page,
        reopen=reopen,
        dialog_id="#lightDialog", name_input="#lightDisplayName", save="#lightSave",
        writes=writes,
        focus_target=lambda: edit,
        row_title=row.locator(".action-row-title"),
        saved_name="Desk left",
    )
    assert len(writes) == 1
    assert writes[0]["url"].endswith("/api/lights/192.0.2.10%3A9123/display_name")
    assert writes[0]["body"] == {
        "display_name": "Desk left", "display_key": "mac:AA:BB:CC:DD:EE:FF",
    }
