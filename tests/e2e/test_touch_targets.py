"""Touch targets the design review measured under the 44px floor (#779).

Native inputs and selects are replaced elements, so the `.hit-target`
``::before`` expansion can't reach them: they get the real 44px box with the
36px control painted inside it. Stacked switches sit on 44px rows so their
tap zones meet instead of overlapping. One representative surface each — the
recipe is shared CSS, so a regression shows up here.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings
from tests.e2e._geometry import assert_min_target, assert_no_overlap, effective_rects


def test_native_controls_and_stacked_switches_meet_the_44px_floor(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    boot_home(page, base_url)
    open_settings(page)

    page.locator("details.presence-settings-card > summary").click()
    inputs = page.locator(".presence-settings .input-native")
    expect(inputs.first).to_be_visible()
    assert_min_target(inputs)
    # The visible control stays the 36px `control` height inside the 44px box.
    for target in effective_rects(inputs):
        assert target.visual.height == 44
    assert inputs.first.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return el.clientHeight === 36 && s.borderTopColor === 'rgba(0, 0, 0, 0)'; }"
    )

    page.locator("details.security-notify-card > summary").click()
    switches = page.locator(".security-notify-card .notify-toggle .toggle")
    expect(switches.first).to_be_visible()
    assert_min_target(switches)
    assert_no_overlap(switches)
