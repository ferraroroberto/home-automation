"""Inline code in the Activity legend is the design system's code-inline chip (#911).

design.md `code-inline`: mono at 0.875em of the text around it (never the 12px
caption), on the neutral-soft fill, 1px 6px padding, 6px radius. The legend's
text sits at body-sm so the chip lands at 12.25px; fleet rule COMP-17 measures
the same thing on the rendered app.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home

_BODY_SM_PX = 14.0
_CHIP_PX = _BODY_SM_PX * 0.875  # 12.25px — not the 12px caption


def _open_legend(page: Page, base_url: str) -> None:
    page.route(
        "**/api/activity**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"domains": ["hvac", "security"]}
                if "/api/activity/domains" in route.request.url
                else {"events": []}
            ),
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabSecurity").click()
    page.locator("#securityEventsAll").click()
    expect(page.locator("#activityDialog")).to_be_visible()
    page.locator(".activity-legend > summary").click()


@pytest.mark.chromium_only
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_legend_code_is_the_code_inline_chip(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
    mock_energy: Callable, mock_security: Callable, mock_presence: Callable,
    theme: str,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_presence()
    _open_legend(page, base_url)
    page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)

    codes = page.locator(".activity-legend-body code")
    assert codes.count() == 3
    # The neutral-soft fill, read through a probe so the check follows the theme.
    fill = page.evaluate(
        """() => {
            const probe = document.createElement('div');
            probe.style.background = 'var(--neutral-soft)';
            document.body.appendChild(probe);
            const v = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return v;
        }"""
    )
    for i in range(codes.count()):
        style = codes.nth(i).evaluate(
            """el => {
                const s = getComputedStyle(el);
                return {size: parseFloat(s.fontSize), family: s.fontFamily,
                        fill: s.backgroundColor, radius: s.borderTopLeftRadius,
                        padding: s.paddingTop + ' ' + s.paddingRight};
            }"""
        )
        assert abs(style["size"] - _CHIP_PX) < 0.05, (theme, i, style)
        assert "monospace" in style["family"], (theme, i, style)
        assert style["fill"] == fill, (theme, i, style, fill)
        assert style["radius"] == "6px", (theme, i, style)
        assert style["padding"] == "1px 6px", (theme, i, style)
