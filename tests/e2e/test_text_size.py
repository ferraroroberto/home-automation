"""Settings text-size control — the vendored Small / Default / Large setting (#820).

Each step must change the root font-size (so every rem-based type role scales),
the choice must survive a reload (the inline boot script stamps it before first
paint, not the module that binds the control), and Large must not push the
layout past the phone viewport.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings
from tests.e2e._geometry import assert_no_horizontal_overflow

# design.md text-size: 93.75% / 100% / 112.5% of the 16px UA root.
_ROOT_PX = {"small": 15.0, "default": 16.0, "large": 18.0}
_STORAGE_KEY = "home-automation.textsize"


def _root_px(page: Page) -> float:
    return float(
        page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    )


def _boot_settings(page: Page, base_url: str, mocks: List[Callable]) -> None:
    for install in mocks:
        install()
    boot_home(page, base_url)
    open_settings(page)


@pytest.mark.chromium_only
def test_each_step_changes_root_font_size_and_persists_across_reload(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    _boot_settings(
        page, base_url,
        [lambda: mock_api(sample_units), mock_energy, mock_security, mock_presence],
    )
    control = page.locator("#textSizeControl")
    expect(control).to_be_visible()
    # Nothing stored yet: the Default step is the pressed one.
    assert page.evaluate("document.documentElement.dataset.textsize") == "default"
    assert _root_px(page) == _ROOT_PX["default"]
    expect(control.locator('[data-textsize="default"]')).to_have_attribute(
        "aria-pressed", "true"
    )

    seen = {}
    for step in ("small", "large", "default"):
        control.locator(f'[data-textsize="{step}"]').click()
        expect(control.locator(f'[data-textsize="{step}"]')).to_have_attribute(
            "aria-pressed", "true"
        )
        seen[step] = _root_px(page)
        assert seen[step] == _ROOT_PX[step], f"{step}: root font-size {seen[step]}px"
        assert page.evaluate("k => localStorage.getItem(k)", _STORAGE_KEY) == step
    assert seen["small"] < seen["default"] < seen["large"]

    # Persistence: Large survives a reload, and is on the root before any module
    # has run (the boot script, not the binding, is what stamps it).
    control.locator('[data-textsize="large"]').click()
    page.reload(wait_until="domcontentloaded")
    assert page.evaluate("document.documentElement.dataset.textsize") == "large"
    assert _root_px(page) == _ROOT_PX["large"]
    page.wait_for_selector("#paneHome", state="visible")
    open_settings(page)
    expect(page.locator('#textSizeControl [data-textsize="large"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def test_large_text_does_not_overflow_the_phone_viewport(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script(
        f"try {{ localStorage.setItem('{_STORAGE_KEY}', 'large'); }} catch (e) {{}}"
    )
    _boot_settings(
        page, base_url,
        [lambda: mock_api(sample_units), mock_energy, mock_security, mock_presence],
    )
    assert _root_px(page) == _ROOT_PX["large"]
    assert_no_horizontal_overflow(page)
    control = page.locator("#textSizeControl")
    box = control.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390
