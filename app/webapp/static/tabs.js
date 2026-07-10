/* Tab switcher: Home | AC | Energy | Plugs | Light | Net | Alarm.
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

export function setTab(tab) {
  if (nav) nav.setTab(tab);
}

export function wireTabs(onTab) {
  nav = initNavTabs({
    storageKey: TAB_KEY,
    navEvent: recordNavEvent,
    onChange: function (tab) {
      state.tab = tab;
      if (onTab) onTab(tab);
    },
  });
}
