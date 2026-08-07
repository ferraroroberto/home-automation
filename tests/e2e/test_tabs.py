"""Tab navigation — the bottom-tab switcher and the floating nav pill.

Pure navigation: pane switching, the retired-tab migration, the saved-tab
restore, and the nav-at-rest watchdog (#229/#232/#420). Per-feature tab
content lives in its own module — `test_home_tab.py`, `test_vm_tile.py`,
`test_ac_tab.py`, `test_energy_tab.py`, `test_security_tab.py`,
`test_cameras.py`, `test_presence.py`.
"""

from __future__ import annotations

from typing import Callable, Dict, List

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home


def test_tab_navigation_switches_panes(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    # Default: Home visible, the other panes hidden.
    expect(page.locator("#paneHome")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy")).to_be_hidden()

    # Home now shows the same Solar → Home ← Grid flow card as Energy (issue #57),
    # revealed once the first /api/energy read lands.
    expect(page.locator("#paneHome .flow-row")).to_be_visible()
    expect(page.locator("#homeFlowPv")).to_have_text("2,500 W")

    # AC tab → unit cards become visible.
    page.locator("#tabAc").click()
    expect(page.locator("#paneAc")).to_be_visible()
    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator(".unit-card").first).to_be_visible()

    # Energy tab → the live flow row shows.
    page.locator("#tabEnergy").click()
    expect(page.locator("#paneEnergy")).to_be_visible()
    expect(page.locator("#paneAc")).to_be_hidden()
    expect(page.locator("#paneEnergy .flow-row")).to_be_visible()


# Computed transform of the floating nav is at its locked rest position iff it
# has no upward translate — 'none' (cleared) or the identity matrix.
_NAV_AT_REST = (
    "() => { const t = getComputedStyle(document.querySelector('.tabs')).transform;"
    " return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'; }"
)


def test_nav_self_heals_when_stranded(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
) -> None:
    """#229: a latched upward transform on the floating bottom-tab pill must be
    repainted back to its locked bottom position by the self-healing watchdog,
    with no app restart. Playwright's WebKit doesn't reproduce iOS Safari's
    collapsing toolbar, so we inject the exact failure mode — a stale
    ``translateY(-Npx)`` — directly, then assert the controller re-derives the
    resting position and clears it."""
    mock_api(sample_units)
    boot_home(page, base_url)

    nav = page.locator(".tabs")
    # Set + read in one evaluate call so the watchdog can't interleave between them.
    strand_transform = nav.evaluate(
        "el => { el.style.transform = 'translateY(-120px)';"
        " return getComputedStyle(el).transform; }"
    )
    assert "120" in strand_transform

    # The ~400ms watchdog re-derives the rest position and clears the strand.
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_nav_not_left_translated_after_modal(
    page: Page, base_url: str, sample_units: List[Dict], mock_api: Callable,
) -> None:
    """#229: opening then closing a detail modal must leave the nav at rest —
    no stranded transform — via both the X button and the Esc key (the path
    that never routed through the app's close handlers and historically left
    the bar stuck up)."""
    mock_api(sample_units)
    boot_home(page, base_url)
    page.locator("#tabAc").click()
    page.wait_for_selector(".unit-card", state="visible")

    # Close via the X button.
    page.locator('[data-unit-id="unit-1"] .unit-header').click()
    expect(page.locator("#detailDialog")).to_be_visible()
    page.locator("#detailClose").click()
    expect(page.locator("#detailDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)

    # Close via Esc.
    page.locator('[data-unit-id="unit-1"] .unit-header').click()
    expect(page.locator("#detailDialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#detailDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_app_restores_saved_short_tab_with_nav_at_rest(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#232: the nav is a body-level sibling of the inner scroller, so the PWA
    can safely restore a short saved tab without floating the fixed bar up."""
    page.add_init_script(
        "localStorage.setItem('home-automation.tab', 'iot');"
    )
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneIot", state="visible")

    expect(page.locator("body > .tabs")).to_have_count(1)
    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator("#tabIot")).to_have_attribute("aria-selected", "true")
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


@pytest.mark.parametrize("retired_tab", ["plugs", "lights"])
def test_retired_tab_selection_migrates_to_iot(
    page: Page, base_url: str, retired_tab: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#136: Plugs and Light folded into IoT. The vendored switcher drops a tab
    name it doesn't recognise and silently falls back to the first tab, so an
    installed PWA parked on either one would reopen on Home. tabs.js rewrites
    the stored key before the switcher reads it."""
    page.add_init_script(
        f"localStorage.setItem('home-automation.tab', '{retired_tab}');"
    )
    mock_api(sample_units)
    mock_energy()
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneIot", state="visible")

    expect(page.locator("#paneHome")).to_be_hidden()
    expect(page.locator("#tabIot")).to_have_attribute("aria-selected", "true")
    # The rewrite is persisted, not just mapped at read time.
    assert page.evaluate("() => localStorage.getItem('home-automation.tab')") == "iot"


def test_nav_at_rest_after_plug_modal_with_autofocus(
    page: Page, base_url: str, sample_plugs: List[Dict], mock_tuya: Callable,
) -> None:
    """#229 follow-up: the plugs rename modal auto-focuses its Display-name input
    (plugs.js), which raises the iOS keyboard and shrinks the visual viewport —
    the one path that still stranded the nav. Opening it (input focused) then
    closing must leave the bar at its locked rest position."""
    mock_tuya(sample_plugs)
    boot_home(page, base_url)
    page.locator("#tabIot").click()
    page.wait_for_selector("#paneIot", state="visible")
    # Rows live inside collapsed <details> cards — expand so they're interactable.
    page.eval_on_selector_all(
        "details.device-list-card", "els => els.forEach(e => { e.open = true; })"
    )

    page.locator('[data-device-id="plug-1"] .device-row-name').click()
    expect(page.locator("#plugDialog")).to_be_visible()
    # The modal auto-focuses the text input — assert that, then close.
    expect(page.locator("#plugDisplayName")).to_be_focused()
    page.locator("#plugDetailClose").click()
    expect(page.locator("#plugDialog")).to_be_hidden()
    page.wait_for_function(_NAV_AT_REST, timeout=3000)


def test_app_padding_owned_by_vendored_nav_on_mobile(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    """#420: on the mobile-pill breakpoint the vendored nav-tabs.css owns
    .app's padding (safe-area top + nav-pill bottom clearance). It loads
    BEFORE styles.css, so an unconditioned ``padding`` shorthand on the base
    .app rule silently wins the cascade and flattens both — the PWA then
    renders under the iOS status bar and behind the floating pill."""
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)

    metrics = page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('.app'));"
        " return { mobilePill: matchMedia('(pointer: coarse) and (max-width: 520px)').matches,"
        " top: parseFloat(s.paddingTop), bottom: parseFloat(s.paddingBottom) }; }"
    )
    if metrics["mobilePill"]:
        # env(safe-area-inset-*) is 0 under emulation, so the floors are
        # --gap (12px) on top and margin+bar+margin+gap (≈115px) below.
        assert metrics["top"] >= 12, metrics
        assert metrics["bottom"] >= 100, metrics
    else:
        # Desktop keeps the styles.css padding: 0 var(--gap) 24px.
        assert metrics["top"] == 0, metrics
        assert metrics["bottom"] == 24, metrics
