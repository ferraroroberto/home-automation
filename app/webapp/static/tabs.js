/* Tab switcher: Home | AC | Energy | Devices | Security (data-tab ids keep
 * their historical 'iot' / 'security' names — only the labels changed).
 * Settings is no tab since #779 (six tabs exceeded the five a bottom bar
 * holds): every page header's gear opens its pane, and choosing any tab
 * leaves it — the app-launcher#1131 shape.
 *
 * Thin adapter over the vendored _vendored/nav/nav-tabs.js (issue #184) — that
 * file owns tab/pane discovery, ARIA + roving tabindex, localStorage
 * persistence, the standalone-PWA .app scroller reset, and the
 * visualViewport pin (browser-tab toolbar only; never a measured translate
 * in standalone — the on-device lessons that shaped it, home-automation
 * #205/#214/#229/#232/#300/#303/#381, now live in that file's own comments).
 * This module only keeps state.tab in sync, opens Settings, and forwards
 * nav-debug's recordNavEvent so the on-device forensics log (#300) keeps
 * working. */

'use strict';

import { state, TAB_KEY } from './state.js';
import { recordNavEvent } from './nav-debug.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

// Tabs folded into 'iot' (issue #136). The vendored switcher drops a stored tab
// name it doesn't recognise and falls back to the first one, so without this an
// installed PWA parked on Plugs or Light silently reopens on Home. Rewriting the
// key up front (rather than mapping at read time) means the migration runs once
// and then costs nothing.
const RETIRED_TABS = ['plugs', 'lights'];
// The Net tab became the Settings pane (#779). Settings is no tab, so it is
// never stored; a PWA parked on Net reopens once on Settings, where its
// content now lives, and the key falls back to Home from then on.
const RETIRED_TO_SETTINGS = 'network';

// The vendored nav (project-scaffolding#338) owns the count badge; set once
// wireTabs has run.
let nav = null;

// A tab's count badge (#880, decision 10 of #872). The vendored setBadge
// paints attention only; `tone` 'danger' marks the tab button so styles.css
// can repaint that one badge, until the nav takes a tone itself
// (project-scaffolding#342). Pass 0 to clear it.
export function setTabBadge(tab, count, noun, tone) {
  if (!nav) return;
  nav.setBadge(tab, count, noun);
  const btn = document.querySelector('nav.tabs .tab[data-tab="' + tab + '"]');
  if (!btn) return;
  if (count > 0 && tone === 'danger') btn.dataset.badgeTone = 'danger';
  else delete btn.dataset.badgeTone;
}

function migrateStoredTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (RETIRED_TABS.includes(stored)) {
      localStorage.setItem(TAB_KEY, 'iot');
    } else if (stored === RETIRED_TO_SETTINGS) {
      localStorage.setItem(TAB_KEY, 'home');
      return true;
    }
  } catch (_) { /* private mode */ }
  return false;
}

// Show the Settings pane over the current tab. The vendored nav only manages
// its own tabs' panes, so this hides them here and leaves no tab selected; the
// nav's next setTab (any tab tap) shows that tab's pane again, and onChange
// below hides this one. Never written to TAB_KEY, so a reload from Settings
// reopens the last real tab.
function openSettings(onTab) {
  const settings = document.getElementById('paneSettings');
  const nav = document.querySelector('nav.tabs');
  if (!settings || !nav) return;
  document.querySelectorAll('main.app > section.pane').forEach(function (pane) {
    pane.hidden = pane !== settings;
  });
  nav.querySelectorAll('.tab[data-tab]').forEach(function (btn) {
    btn.classList.remove('active');
    btn.setAttribute('aria-selected', 'false');
  });
  nav.dataset.activeTab = 'settings';
  state.tab = 'settings';
  const scroller = document.querySelector('.app');
  if (scroller) scroller.scrollTop = 0;
  window.scrollTo(0, 0);
  if (onTab) onTab('settings');
}

export function wireTabs(onTab) {
  const openSettingsNow = migrateStoredTab();
  nav = initNavTabs({
    storageKey: TAB_KEY,
    navEvent: recordNavEvent,
    onChange: function (tab) {
      state.tab = tab;
      const settings = document.getElementById('paneSettings');
      if (settings) settings.hidden = true;
      if (onTab) onTab(tab);
    },
  });
  document.querySelectorAll('.settings-open-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { openSettings(onTab); });
  });
  if (openSettingsNow) openSettings(onTab);
}
