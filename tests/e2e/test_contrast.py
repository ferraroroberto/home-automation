"""Text contrast on tints and solid fills, both themes (#779, COLOR-02).

A hue used as text and as a fill needs two tokens (design.md): the base hue on
its own 16% tint falls under 4.5:1, and white on the dark theme's base accent
is 3.75:1. Tinted controls set their text in the `*-text` role and the solid
primary fills with `--accent-fill`. One representative per role — the colours
are shared tokens, so a regression in a role shows up here.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page

from tests.e2e._app import boot_home

# Contrast of an element's text against its composited background. Colours are
# resolved through a canvas so oklch()/color() (the P3 twins) parse too.
_CONTRAST_JS = """el => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const rgba = c => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = c;
    cx.fillRect(0, 0, 1, 1); const d = cx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (t, b) => [0, 1, 2].map(i => t[i] * t[3] + b[i] * (1 - t[3])).concat(1);
  const layers = [];
  for (let n = el; n; n = n.parentElement) {
    const c = rgba(getComputedStyle(n).backgroundColor); if (c[3] > 0) layers.push(c); }
  let bg = [255, 255, 255, 1];
  for (let i = layers.length - 1; i >= 0; i--) bg = over(layers[i], bg);
  const fg = over(rgba(getComputedStyle(el).color), bg);
  const lum = c => c.slice(0, 3).map(v => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); })
    .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
  const [x, y] = [lum(fg), lum(bg)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tinted_and_solid_controls_clear_aa(
    page: Page, base_url: str, theme: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
) -> None:
    page.add_init_script(f"localStorage.setItem('home-automation.theme', '{theme}')")
    mock_api(sample_units)
    mock_energy()
    mock_security()
    boot_home(page, base_url)

    # The segmented alarm control (#879, #888): every mode is fg, on the
    # neutral-soft track or (selected) on the card. Disarmed selects Off.
    page.wait_for_selector("#homeSecurityActions .security-action-arm:not(:disabled)")
    for selector in (".security-action-disarm", ".security-action-partial", ".security-action-arm"):
        ratio = page.locator(f"#homeSecurityActions {selector}").evaluate(_CONTRAST_JS)
        assert ratio >= 4.5, (theme, selector, ratio)

    # fg on the raised card segment: the selected period.
    page.locator("#tabEnergy").click()
    ratio = page.locator('#paneEnergy .segmented-item[aria-pressed="true"]').first.evaluate(_CONTRAST_JS)
    assert ratio >= 4.5, (theme, "segmented-item selected", ratio)

    # White on the solid primary fill.
    page.evaluate("() => document.getElementById('reminderDialog').showModal()")
    ratio = page.locator("#reminderSave").evaluate(_CONTRAST_JS)
    assert ratio >= 4.5, (theme, "#reminderSave", ratio)
