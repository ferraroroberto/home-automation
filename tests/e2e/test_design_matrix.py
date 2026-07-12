"""Viewport × theme matrix conformance for the home view (issue #428).

Runs the vendored rendered-geometry helper's 320/390/430/772px × light/dark
``MATRIX`` (the deferred live-app verification leg of project-scaffolding#157)
against the key `/` view. Each leg re-applies the theme through this app's
own persistence mechanism — the ``home-automation.theme`` localStorage key the
inline boot script in ``index.html`` reads — then boot-checks that the theme
actually took (the token-driven body background flipped) before asserting the
layout stays fluid. This is the repo's first automated dark-theme e2e; the
#409 dark × multi-width sweep was manual on-device work.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

import pytest
from playwright.sync_api import Page

from tests.e2e._geometry import (
    MATRIX,
    apply_matrix_leg,
    assert_no_horizontal_overflow,
    matrix_id,
)

# The app's own persistence key (state.js THEME_KEY, read by the index.html
# boot script) and the authored --bg token per theme (styles.css :root /
# [data-theme="dark"]) — the boot-check that a leg's theme actually applied.
_THEME_KEY = "home-automation.theme"
_EXPECTED_BG = {"light": "rgb(255, 255, 255)", "dark": "rgb(13, 17, 23)"}


def _set_theme_via_local_storage(page: Page, theme: str) -> None:
    """Drive the app's real theme boot: persist the key, then reload so the
    inline boot script (and main.js initTheme) applies it — never stamp
    ``dataset.theme`` directly, that would bypass the mechanism under test."""
    page.evaluate(
        "([key, theme]) => localStorage.setItem(key, theme)", [_THEME_KEY, theme]
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")


@pytest.mark.parametrize("leg", MATRIX, ids=matrix_id)
def test_home_view_is_fluid_across_viewport_theme_matrix(
    leg: Tuple[int, str], page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_presence: Callable,
) -> None:
    width, theme = leg
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")

    apply_matrix_leg(page, width, theme, set_theme=_set_theme_via_local_storage)

    # Boot-check: the leg's theme really applied — fail loud, never let a
    # silently-light "dark" leg read as conformance.
    assert page.evaluate("document.documentElement.dataset.theme") == theme
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == _EXPECTED_BG[theme], (
        f"{width}px/{theme}: body background {background} does not match the "
        f"authored --bg token {_EXPECTED_BG[theme]} — theme did not apply"
    )

    # Home renders its summary content before we measure the layout.
    page.wait_for_selector("#paneHome .flow-row", state="visible")
    assert_no_horizontal_overflow(page)
