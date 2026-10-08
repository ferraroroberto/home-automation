"""Cameras in the Security pane — list states, and the live view's control layout.

The list-state tests are DOM-state only (`chromium_only`); the live-view layout
test runs on both engines, since the iPhone is WebKit (#859).
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, hold_reads
from tests.e2e._geometry import assert_min_target, assert_no_overlap

_CAMERA = {
    "id": "front-door",
    "display_name": "Front door",
    "reachable": True,
    "model": "Fixture camera",
    "recording": False,
    "ptz_presets": False,
    "ptz_absolute": False,
}


def _open_live_view(page: Page, base_url: str, camera: Dict, presets: List[Dict]) -> None:
    """Open the live view on a stubbed camera; no request reaches a real one.

    The catch-all is registered first so the later, specific routes win; it
    answers the stream, PTZ and anything else under a camera with an empty 204.
    """
    page.route("**/api/cameras/*/**", lambda route: route.fulfill(status=204))
    page.route(
        "**/api/cameras/*/presets",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"presets": presets}),
        ),
    )
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"cameras": [camera]}),
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator(".cameras-card > summary").click()
    live = page.locator("#camerasList .camera-row-live")
    expect(live).to_have_text("Live")
    live.click()
    expect(page.locator("#cameraLiveDialog")).to_be_visible()


# Per labelled control: does its content (text + glyph) stay inside the
# button, and on how many lines does its text sit?
_LABEL_FIT_JS = """els => els.map(el => {
  const box = el.getBoundingClientRect();
  const all = document.createRange();
  all.selectNodeContents(el);
  const content = all.getBoundingClientRect();
  const tops = new Set();
  for (const node of el.childNodes) {
    if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) tops.add(Math.round(rect.top));
  }
  return {
    label: el.textContent.trim(),
    clipped: content.left < box.left - 0.5 || content.right > box.right + 0.5,
    lines: tops.size,
  };
})"""

_CENTRE_JS = "el => { const r = el.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }"

_LIVE_LABELS = ["Step", "Zoom out", "Zoom in", "Screenshot", "Record"]


@pytest.mark.chromium_only
def test_cameras_distinguish_loading_from_true_empty(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"cameras":[]}',
        ),
    )
    release = hold_reads(page, "/api/cameras")
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "loading")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Reading cameras…"
    )
    release()
    expect(page.locator("#camerasList")).to_have_attribute("data-state", "empty")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "No cameras configured"
    )


@pytest.mark.chromium_only
def test_cameras_show_contextual_unavailable_state(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.route(
        "**/api/cameras",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "error")
    expect(page.locator("#camerasList .empty-state-message")).to_have_text(
        "Cameras unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("192.0.2.50")


@pytest.mark.chromium_only
def test_camera_refresh_failure_preserves_last_good_rows(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    failing = {"value": False}

    def handle_cameras(route) -> None:
        if failing["value"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"camera 192.0.2.50 timed out after 10 seconds"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"cameras": [_CAMERA]}),
        )

    page.route("**/api/cameras", handle_cameras)
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)

    failing["value"] = True
    page.locator("#tabHome").click()
    page.locator("#tabSecurity").click()

    expect(page.locator("#camerasList")).to_have_attribute("data-state", "stale")
    expect(page.locator("#camerasList .camera-row")).to_have_count(1)
    expect(page.locator("#camerasNote")).to_contain_text("Last updated")
    expect(page.locator("#camerasNote")).to_contain_text("live data unavailable")
    expect(page.locator("#camerasNote")).not_to_contain_text("192.0.2.50")


@pytest.mark.chromium_only
def test_camera_buttons_carry_a_visible_label(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    """#805: no icon-only camera button — the row's Live and every live-view action."""
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _open_live_view(page, base_url, _CAMERA, [])

    actions = page.locator("#cameraLiveDialog .camera-live-controls button:not(.camera-ptz-btn)")
    expect(actions).to_have_text(_LIVE_LABELS)


@pytest.mark.parametrize("width", [320, 390])
def test_camera_live_controls_fit_a_phone(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable, width: int,
) -> None:
    """#859: at phone width every live-view control is a full, separate 44px
    target, no label wraps or spills out of its button, nothing leaves the
    card, and the pad's arms sit on one cross."""
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.set_viewport_size({"width": width, "height": 844})
    presets = [{"token": "1", "name": "Door"}, {"token": "2", "name": "Garden"}]
    _open_live_view(page, base_url, dict(_CAMERA, ptz_presets=True), presets)
    dialog = page.locator("#cameraLiveDialog")
    expect(dialog.locator(".camera-preset")).to_have_count(2)

    controls = dialog.locator(".camera-live-controls button, .camera-presets button")
    assert_min_target(controls)
    assert_no_overlap(controls)

    fits = dialog.locator(".camera-live-controls .range-tab").evaluate_all(_LABEL_FIT_JS)
    assert [fit["label"] for fit in fits] == _LIVE_LABELS
    for fit in fits:
        assert not fit["clipped"], f"{fit['label']!r} spills out of its button"
        assert fit["lines"] == 1, f"{fit['label']!r} wraps onto {fit['lines']} lines"

    card = dialog.locator(".camera-live-card")
    assert card.evaluate("el => el.scrollWidth <= el.clientWidth"), "the live view scrolls sideways"
    card_box = card.bounding_box()
    assert card_box is not None
    for control in controls.all():
        box = control.bounding_box()
        assert box is not None
        assert card_box["x"] <= box["x"] and box["x"] + box["width"] <= card_box["x"] + card_box["width"], (
            f"a control at x={box['x']:g} w={box['width']:g} leaves the card"
        )

    up, down, left, right = (
        dialog.locator(f"#cameraPtz{arm}").evaluate(_CENTRE_JS)
        for arm in ("Up", "Down", "Left", "Right")
    )
    assert abs(up[0] - down[0]) < 0.5, "tilt up and down are not on one vertical axis"
    assert abs(left[1] - right[1]) < 0.5, "pan left and right are not on one horizontal axis"
