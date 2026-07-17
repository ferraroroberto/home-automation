/* Tab switcher: Home | AC | Energy | IoT | Net | Alarm.
 *
 * Thin adapter over the vendored _vendored/nav/nav-tabs.js (issue #184) — that
 * file owns tab/pane discovery, ARIA + roving tabindex, localStorage
 * persistence, the standalone-PWA .app scroller reset, and the
 * visualViewport pin (browser-tab toolbar only; never a measured translate
 * in standalone — the on-device lessons that shaped it, home-automation
 * #205/#214/#229/#232/#300/#303/#381, now live in that file's own comments).
 * This module only keeps state.tab in sync and forwards nav-debug's
 * recordNavEvent so the on-device forensics log (#300) keeps working. */

'use strict';

import { state, TAB_KEY } from './state.js';
import { recordNavEvent } from './nav-debug.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

let nav = null;

// Tabs folded into 'iot' (issue #136). The vendored switcher drops a stored tab
// name it doesn't recognise and falls back to the first one, so without this an
// installed PWA parked on Plugs or Light silently reopens on Home. Rewriting the
// key up front (rather than mapping at read time) means the migration runs once
// and then costs nothing.
const RETIRED_TABS = ['plugs', 'lights'];

function migrateStoredTab() {
  try {
    if (RETIRED_TABS.includes(localStorage.getItem(TAB_KEY))) {
      localStorage.setItem(TAB_KEY, 'iot');
    }
  } catch (_) { /* private mode */ }
}

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

export function wireTabs(onTab) {
  migrateStoredTab();
  nav = initNavTabs({
    storageKey: TAB_KEY,
    navEvent: recordNavEvent,
    onChange: function (tab) {
      state.tab = tab;
      if (onTab) onTab(tab);
    },
  });
}
