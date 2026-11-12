"""Floating bottom-tab bar geometry per tab on a 390x844 phone.

An installed iOS app anchors `position: fixed` to the physical screen bottom
only while the document is scrollable, so a tab shorter than the screen left
the bar floating higher than on Home. Desktop engines don't reproduce that
anchoring, so beyond the bar's measured offset this pins the mechanism that
prevents it: every tab's document scrolls by at least 1px. It also pins the
scroll-edge fade bands at the top and under the bar. Runs on Chromium and WebKit.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page

from tests.e2e._app import boot_home
from tests.e2e._flow import run_flow

_TABS = ("home", "ac", "energy", "iot", "security")

_MEASURE_JS = """() => {
  const bar = document.querySelector('.tabs').getBoundingClientRect();
  const root = document.scrollingElement;
  return {
    pill: matchMedia('(pointer: coarse) and (max-width: 520px)').matches,
    barBottomOffset: window.innerHeight - bar.bottom,
    overflow: root.scrollHeight - root.clientHeight,
  };
}"""

_FADE_JS = """pseudo => {
  const s = getComputedStyle(document.body, pseudo);
  const bar = getComputedStyle(document.querySelector('.tabs'));
  return {
    position: s.position, top: s.top, bottom: s.bottom, height: parseFloat(s.height),
    image: s.backgroundImage, mask: s.maskImage || s.webkitMaskImage, pointerEvents: s.pointerEvents,
    z: Number(s.zIndex), barZ: Number(bar.zIndex),
  };
}"""


@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    # A phone with a coarse pointer on both engines, so the floating bar applies.
    return {
        **browser_context_args,
        "viewport": {"width": 390, "height": 844},
        "screen": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
    }


def _bar_sits_at_the_same_offset_on_every_tab(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    offsets: Dict[str, float] = {}
    for tab in _TABS:
        page.locator(f".tabs .tab[data-tab='{tab}']").click()
        page.wait_for_selector(f".tabs .tab[data-tab='{tab}'].active")
        m = page.evaluate(_MEASURE_JS)
        assert m["pill"], m
        # The scrollable document is what keeps iOS anchoring the bar to the
        # screen bottom on a short tab.
        assert m["overflow"] >= 1, (tab, m)
        offsets[tab] = round(m["barBottomOffset"], 1)

    assert offsets["home"] == 21, offsets
    assert set(offsets.values()) == {offsets["home"]}, offsets


def _scroll_edge_fades_top_and_bottom(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    bottom = page.evaluate(_FADE_JS, "::before")
    assert bottom["position"] == "fixed", bottom
    assert bottom["bottom"] == "0px", bottom
    # The 24px tail above the bar + the 61px bar + its 21px bottom margin.
    assert bottom["height"] == 24 + 61 + 21, bottom

    top = page.evaluate(_FADE_JS, "::after")
    assert top["position"] == "fixed", top
    assert top["top"] == "0px", top
    # One thin band from the viewport top (safe area 0 here): the nav's
    # --nav-fade-band, 16px in the owner-validated top-bar recipe.
    assert top["height"] == 16, top

    # The bottom fade paints a gradient; the top glass is the page colour
    # under a blur whose band is faded out by a gradient mask.
    assert "linear-gradient" in bottom["image"], bottom
    assert "linear-gradient" in top["mask"], top
    for fade in (bottom, top):
        assert fade["pointerEvents"] == "none", fade
        assert fade["z"] < fade["barZ"], fade


@pytest.mark.chromium_only
def _installed_app_anchors_the_bar_from_the_large_viewport_top(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """In the installed app iOS grows the viewport after launch, so the bar is
    placed from the top with 100lvh (its final position on frame one) rather
    than from the moving bottom edge. No engine here emulates the standalone
    display mode, so the nav's own standalone rules are applied unconditionally."""
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    m = page.evaluate("""() => {
      const found = [];
      const walk = rules => {
        for (const r of rules) {
          if (r.media && r.conditionText.includes('display-mode: standalone')) found.push(r);
          else if (r.cssRules) walk(r.cssRules);
        }
      };
      for (const sheet of document.styleSheets) {
        if (sheet.href && sheet.href.includes('nav-tabs.css')) walk(sheet.cssRules);
      }
      // A constructed sheet, not an inline <style>, which the page's CSP refuses.
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(found.map(r => Array.from(r.cssRules, c => c.cssText).join(' ')).join(' '));
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
      // Grow the bar: a top-anchored bar keeps its top edge, a bottom-anchored
      // one keeps its bottom edge.
      const bar = document.querySelector('.tabs');
      const before = bar.getBoundingClientRect();
      bar.style.height = (before.height + 40) + 'px';
      const after = bar.getBoundingClientRect();
      bar.style.height = '';
      return { blocks: found.length, topBefore: before.top, topAfter: after.top, lvh: innerHeight };
    }""")
    assert m["blocks"] == 1, m
    assert m["topAfter"] == m["topBefore"], m
    # 21px margin + 61px bar above the large-viewport bottom.
    assert m["topBefore"] == m["lvh"] - 82, m


def _document_stays_scrollable_under_an_open_dialog(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """scroll-lock.js pins the body while a dialog is open; if that left the
    document unscrollable, an installed iOS app would shrink its viewport and
    re-grow it over ~3s on close, revealing the bar bit by bit."""
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    page.locator(".tabs .tab[data-tab='ac']").click()
    page.locator('#acUnits [data-unit-id="unit-1"] .action-row-main').click()
    page.wait_for_function("() => document.body.style.position === 'fixed'")

    overflow = page.evaluate(
        "() => document.scrollingElement.scrollHeight - document.scrollingElement.clientHeight"
    )
    assert overflow >= 1, overflow


def test_nav_offset(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """Floating nav bar: same offset on every tab, scroll fades, installed-app anchor, scrollable under a dialog."""
    run_flow(
        request, page, base_url,
        _bar_sits_at_the_same_offset_on_every_tab,
        _scroll_edge_fades_top_and_bottom,
        _installed_app_anchors_the_bar_from_the_large_viewport_top,
        _document_stays_scrollable_under_an_open_dialog,
    )
