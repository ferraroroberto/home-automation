/* Tab switcher: Home | AC | Energy | Devices | Security (data-tab ids keep
 * their historical 'iot' / 'security' names — only the labels changed).
 * Settings is no tab (six tabs exceed the five a bottom bar
 * holds): every page header's gear opens its pane, and choosing any tab
 * leaves it.
 *
 * Thin adapter over the vendored _vendored/nav/nav-tabs.js — that
 * file owns tab/pane discovery, ARIA + roving tabindex, localStorage
 * persistence, the standalone-PWA .app scroller reset, and the
 * visualViewport pin (browser-tab toolbar only; never a measured translate
 * in standalone — the on-device lessons that shaped it, live in that file's own comments).
 * This module only keeps state.tab in sync, opens Settings, and forwards
 * nav-debug's recordNavEvent so the on-device forensics log keeps
 * working. */

'use strict';

import { state, TAB_KEY } from './state.js';
import { recordNavEvent } from './nav-debug.js';
import { initNavTabs } from './_vendored/nav/nav-tabs.js';

// Tabs folded into 'iot'. The vendored switcher drops a stored tab
// name it doesn't recognise and falls back to the first one, so without this an
// installed PWA parked on Plugs or Light silently reopens on Home. Rewriting the
// key up front (rather than mapping at read time) means the migration runs once
// and then costs nothing. The Net tab joins them: its content is the Devices tab's
// Network group.
const RETIRED_TABS = ['plugs', 'lights', 'network'];

// The vendored nav owns the count badge; set once
// wireTabs has run.
let nav = null;
// wireTabs' tab-change handler, kept so showSettings can run it too.
let tabHandler = null;

// A tab's count badge, painted by the vendored
// nav's setBadge: `tone` 'danger' repaints that one badge in the danger fill,
// anything else is attention. Pass 0 to clear it.
export function setTabBadge(tab, count, noun, tone) {
  if (!nav) return;
  nav.setBadge(tab, count, noun, tone);
}

function migrateStoredTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (RETIRED_TABS.includes(stored)) localStorage.setItem(TAB_KEY, 'iot');
  } catch (_) { /* private mode */ }
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

// Open Settings from a link on a tab (the Energy tab's Export rate row);
// the caller then opens the row's sheet (settings.js).
export function showSettings() {
  openSettings(tabHandler);
}

// Open a tab from a link on another one (Home's House card): the nav
// switches as if its tab were tapped, then `target`, the part of that tab the
// link is about, scrolls into view.
export function showTab(tab, target) {
  if (!nav) return;
  nav.setTab(tab);
  if (target) requestAnimationFrame(function () { target.scrollIntoView({ block: 'start' }); });
}

export function wireTabs(onTab) {
  tabHandler = onTab;
  migrateStoredTab();
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
}
