"""AC tab pane states — loading, true-empty, unavailable, stale, and snapshot.

Card-level controls and the detail modal have their own modules
(`test_controls.py`, `test_detail_modal.py`); this one covers the pane's own
`data-state` machine and the cached-snapshot paint (#522).
"""

from __future__ import annotations

import copy
import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

from tests.e2e._app import boot_home


def test_ac_tab_distinguishes_loading_from_true_empty(
    page: Page, base_url: str, mock_energy: Callable,
) -> None:
    mock_energy()
    page.add_init_script("""
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/units' || url.endsWith('/api/units')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({units: []}), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 750);
            });
          }
          return originalFetch(input, init);
        };
    """)
    boot_home(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "loading")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "Reading AC units…"
    )
    expect(page.locator("#paneAc")).to_have_attribute("data-state", "empty")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "No AC units configured"
    )


def test_ac_tab_shows_contextual_unavailable_state(
    page: Page, base_url: str, mock_energy: Callable,
) -> None:
    mock_energy()
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"melcloud.example.internal timed out after 10 seconds"}',
        ),
    )
    boot_home(page, base_url)
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "error")
    expect(page.locator("#acFeedback .empty-state-message")).to_have_text(
        "AC units unavailable"
    )
    expect(page.locator("#toast")).not_to_contain_text("melcloud.example.internal")


def test_ac_poll_failure_preserves_units_and_disables_actions(
    page: Page, base_url: str, sample_units: List[Dict],
    mock_api: Callable, mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    boot_home(page, base_url)
    page.locator("#tabAc").click()
    expect(page.locator(".unit-card")).to_have_count(len(sample_units))

    page.unroute("**/api/units")
    page.route(
        "**/api/units",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail":"melcloud.example.internal timed out after 10 seconds"}',
        ),
    )
    page.locator("#tabEnergy").click()
    page.locator("#tabAc").click()

    expect(page.locator("#paneAc")).to_have_attribute("data-state", "stale")
    expect(page.locator("#acFeedback")).to_contain_text("Last updated")
    expect(page.locator("#acFeedback")).to_contain_text("live data unavailable")
    expect(page.locator(".unit-card")).to_have_count(len(sample_units))
    expect(page.locator("#unitsGrid button:enabled, #unitsGrid select:enabled")).to_have_count(0)
    expect(page.locator("#acSummary .ac-line-toggle:enabled")).to_have_count(0)
    expect(page.locator("#acFeedback")).not_to_contain_text("melcloud.example.internal")


def test_units_snapshot_paints_before_live_refresh(
    page: Page, base_url: str, sample_units: List[Dict], mock_energy: Callable,
) -> None:
    cached_units = copy.deepcopy(sample_units)
    live_units = copy.deepcopy(sample_units)
    cached_units[0]["name"] = "Snapshot Office"
    live_units[0]["name"] = "Live Office"
    snapshot_store = {
        "version": 1,
        "snapshots": {
            "units": {
                "saved_at": "2026-06-24T20:15:00.000Z",
                "body": {"units": cached_units},
            },
        },
    }
    page.add_init_script("""
        const snapshotStore = %s;
        const liveUnits = %s;
        localStorage.setItem('home-automation.apiSnapshots.v1', JSON.stringify(snapshotStore));
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : input.url;
          if (url === '/api/units' || url.endsWith('/api/units')) {
            return new Promise(function(resolve) {
              setTimeout(function() {
                resolve(new Response(JSON.stringify({units: liveUnits}), {
                  status: 200,
                  headers: {'Content-Type': 'application/json'},
                }));
              }, 1000);
            });
          }
          return originalFetch(input, init);
        };
    """ % (json.dumps(snapshot_store), json.dumps(live_units)))
    mock_energy()
    boot_home(page, base_url)

    # #522: no per-card pill — the cached-vs-live swap is communicated once,
    # via the existing pane-level thin stale-note line (renderAcFeedback()).
    expect(page.locator("#acSummary")).to_contain_text("Snapshot Office")
    expect(page.locator("#acFeedback")).to_contain_text("Last saved")

    expect(page.locator("#acSummary")).to_contain_text("Live Office", timeout=4000)
    expect(page.locator("#acSummary")).not_to_contain_text("Snapshot Office")
    expect(page.locator("#acFeedback")).not_to_contain_text("Last saved")
