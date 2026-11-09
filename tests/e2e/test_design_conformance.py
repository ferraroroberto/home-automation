"""Design conformance: contrast, shared parts, touch targets, legend chip, fluid matrix.

One flow over the rendered design contract. Text contrast on tinted and solid
fills clears AA in both themes (COLOR-02): a hue used as text and as a fill
needs two tokens, so the base hue on its own 16% tint falls under 4.5:1.
The shared parts (header lines, the security badge, sheet save models) read
as one system. Native inputs, stacked switches, card toolbars and light
controls meet the 44px floor. The Activity legend's inline code is the design
system's code-inline chip (mono at 0.875em on the neutral-soft fill). Home has
no horizontal overflow across the viewport x theme matrix of `_geometry.py`.
Each is a rendered-geometry or rendered-colour check of shared tokens and
recipes, so one representative surface each is enough.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Tuple

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home, open_settings_sheet
from tests.e2e._flow import run_flow
from tests.e2e._geometry import (
    MATRIX,
    apply_matrix_leg,
    assert_min_target,
    assert_no_horizontal_overflow,
    assert_no_overlap,
    effective_rects,
)

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


def _tinted_and_solid_controls_clear_aa(
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


_ZONE = {"type": 1, "status": "closed", "active": False, "bypass": False,
         "triggered": False, "display_name": None, "hidden": False}


def _security(mode: str, troubled: int) -> Dict:
    zones = [{**_ZONE, "id": i, "name": f"Zone {i}", "trouble": i <= troubled}
             for i in range(1, 4)]
    return {"reachable": True, "label": mode.title(), "mode": mode,
            "supported_actions": ["partial", "perimeter", "arm"],
            "ac_lost": False, "assumed_control_panel_state": False, "zones": zones}


@pytest.mark.chromium_only
def _header_lines_name_exceptions_else_a_plain_fact(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    mock_security: Callable, mock_presence: Callable,
) -> None:
    sample_units[1]["reachable"] = False  # one unit offline
    mock_api(sample_units)
    mock_energy()  # exporting 1,200 W
    mock_tuya(sample_plugs)  # one switch on, 1,450 W metered
    mock_presence()
    mock_security(_security("disarmed", troubled=1))
    page.route("**/api/ups", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"ups": null}'))
    boot_home(page, base_url)

    ac = page.locator('#paneAc .status[data-head="ac"]')
    expect(ac).to_have_text("1 offline")
    expect(ac.locator(".head-exception")).to_have_attribute("data-tone", "attention")

    page.locator("#tabEnergy").click()
    energy = page.locator('#paneEnergy .status[data-head="energy"]')
    expect(energy).to_have_text("Exporting 1,200 W")
    expect(energy.locator(".head-exception")).to_have_count(0)

    page.locator("#tabIot").click()
    expect(page.locator('#paneIot .status[data-head="iot"]')).to_have_text("1 on · 1,450 W")

    page.locator("#tabSecurity").click()
    security = page.locator('#paneSecurity .status[data-head="security"]')
    expect(security).to_have_text("1 trouble")
    # The exception is its tone's text colour, not the slot's muted grey.
    part = security.locator('.head-exception[data-tone="attention"]')
    assert part.evaluate("el => getComputedStyle(el).color") != security.evaluate(
        "el => getComputedStyle(el).color")


def _security_badge_counts_trouble_and_turns_danger_when_triggered(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
    mock_security: Callable, mock_presence: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_presence()
    mock_security(_security("disarmed", troubled=2))
    boot_home(page, base_url)

    tab = page.locator("#tabSecurity")
    badge = tab.locator(".tab-badge")
    expect(badge).to_have_text("2")
    expect(tab).to_have_accessible_name(re.compile(r"Security, 2 trouble"))
    expect(badge).to_have_attribute("data-tone", "attention")
    attention_bg = badge.evaluate("el => getComputedStyle(el).backgroundColor")

    # Triggered outranks trouble: one danger badge, still AA on its fill.
    mock_security(_security("triggered", troubled=2))
    page.reload(wait_until="domcontentloaded")
    expect(badge).to_have_text("1")
    expect(badge).to_have_attribute("data-tone", "danger")
    expect(tab).to_have_accessible_name(re.compile(r"Security, 1 triggered"))
    assert badge.evaluate("el => getComputedStyle(el).backgroundColor") != attention_bg
    assert badge.evaluate(_CONTRAST_JS) >= 4.5

    # Nothing wrong: no badge at all.
    mock_security(_security("armed", troubled=0))
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#homeSecurityActions .security-action")
    expect(badge).to_have_count(0)


@pytest.mark.chromium_only
def _sheets_follow_their_save_model(
    page: Page, base_url: str, sample_units: List[Dict], sample_plugs: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_tuya: Callable,
    mock_network: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    mock_tuya(sample_plugs)
    mock_network()
    boot_home(page, base_url)

    # Staged: the plug editor. Esc and the backdrop discard the draft, and
    # focus goes back to the row that opened it.
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    opener = page.locator('[data-device-id="plug-1"] .action-row-main')
    dialog = page.locator("#plugDialog")
    expect(dialog).to_have_attribute("data-save-model", "staged")
    opener.click()
    expect(dialog).to_be_visible()
    page.locator("#plugDisplayName").fill("Draft name")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    opener.click()
    expect(page.locator("#plugDisplayName")).to_have_value("")
    page.mouse.click(4, 4)  # the backdrop
    expect(dialog).to_be_hidden()
    expect(opener.locator(".action-row-title")).to_have_text("Test Heater")

    # Instant: the Network sheet (Devices since #884) and the Wi-Fi sheet on
    # top of it save as they change; their one primary is Done.
    page.locator("#networkInternetOpen").click()
    expect(page.locator("#networkSheet")).to_have_attribute("data-save-model", "instant")
    page.locator("details.net-wifi-card > summary").click()
    page.locator("#netWifiList .net-wifi-row").filter(has_text="TestNet-IoT") \
        .locator(".net-wifi-row-name").click()
    wifi = page.locator("#netWifiDialog")
    expect(wifi).to_have_attribute("data-save-model", "instant")
    page.locator("#netWifiDisplayName").fill("Neighbour AP")
    page.locator("#netWifiDone").click()
    expect(wifi).to_be_hidden()
    expect(page.locator("#netWifiList .net-wifi-row").filter(has_text="Neighbour AP")).to_have_count(1)


def _native_controls_and_stacked_switches_meet_the_44px_floor(
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
    open_settings_sheet(page, "homeLocationSheet")
    inputs = page.locator("#homeLocationSheet .presence-settings .input-native")
    expect(inputs.first).to_be_visible()
    assert_min_target(inputs)
    # The visible control stays the 36px `control` height inside the 44px box.
    for target in effective_rects(inputs):
        assert target.visual.height == 44
    assert inputs.first.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return el.clientHeight === 36 && s.borderTopColor === 'rgba(0, 0, 0, 0)'; }"
    )

    page.locator("#homeLocationSheet [data-sheet-done]").click()
    page.locator('#paneSettings [data-settings-sheet="notifyAlarmSheet"]').click()
    switches = page.locator("#notifyAlarmSheet .notify-toggle .toggle")
    expect(switches.first).to_be_visible()
    assert_min_target(switches)
    assert_no_overlap(switches)


def _card_toolbar_and_light_controls_keep_their_own_tap_zones(
    page: Page, base_url: str, sample_units: List[Dict], sample_lights: List[Dict],
    mock_api: Callable, mock_energy: Callable, mock_security: Callable,
    mock_lights: Callable,
) -> None:
    """The live review caught what the fixtures hide: a body toolbar's "Show
    hidden" tab reaching into the first row's switch, and the light rows'
    sliders and number fields under the floor (#779; in the light sheet since
    #884)."""
    page.set_viewport_size({"width": 390, "height": 844})
    mock_api(sample_units)
    mock_energy()
    mock_security()
    mock_lights(sample_lights)
    boot_home(page, base_url)

    page.locator("#tabSecurity").click()
    # The footer toggle only renders once a detector is hidden; show it directly.
    page.evaluate("() => { document.getElementById('securityHiddenToggle').hidden = false; }")
    first_switch = page.locator("#securityZones .security-zone .toggle").first
    expect(first_switch).to_be_visible()
    assert_no_overlap([page.locator("#securityHiddenToggle"), first_switch])

    # The sliders and number fields live in the light sheet since #884.
    page.locator("#tabIot").click()
    page.locator('#lightsList .light-row:not(.is-unavailable) .action-row-main').first.click()
    reachable = page.locator("#lightSheet .light-slider")
    expect(reachable.first).to_be_visible()
    assert_min_target(reachable)
    assert_min_target(page.locator("#lightSheet .light-number"))


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
def _legend_code_is_the_code_inline_chip(
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


# The app's own persistence key (state.js THEME_KEY, read by the index.html
# boot script) and the authored --bg token per theme (styles/base.css :root /
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


def _home_view_is_fluid_across_viewport_theme_matrix(
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


def test_design_conformance(
    page: Page, base_url: str, request: pytest.FixtureRequest,
) -> None:
    """Design contract: AA contrast, shared parts, 44px targets, legend code chip, fluid Home across the viewport x theme matrix."""
    run_flow(
        request, page, base_url,
        (_tinted_and_solid_controls_clear_aa, {'theme': 'light'}),
        (_tinted_and_solid_controls_clear_aa, {'theme': 'dark'}),
        _header_lines_name_exceptions_else_a_plain_fact,
        _security_badge_counts_trouble_and_turns_danger_when_triggered,
        _sheets_follow_their_save_model,
        _native_controls_and_stacked_switches_meet_the_44px_floor,
        _card_toolbar_and_light_controls_keep_their_own_tap_zones,
        (_legend_code_is_the_code_inline_chip, {'theme': 'light'}),
        (_legend_code_is_the_code_inline_chip, {'theme': 'dark'}),
        *[(_home_view_is_fluid_across_viewport_theme_matrix, {"leg": leg}) for leg in MATRIX],
    )
