/* Tab switcher: Home | AC | Energy | Plugs | Security.
 *
 * Mirrors app-launcher's nav.tabs pattern: .tab buttons get .active, .pane
 * sections toggle via [hidden]. The chosen tab is remembered in localStorage
 * so an installed PWA reopens where you left it. main.js registers an
 * onChange hook so the Energy tab can spin up its charts + faster polling. */

'use strict';

import { els, state, TAB_KEY } from './state.js';

const TABS = ['home', 'ac', 'energy', 'plugs', 'security'];
// tab name → button el handle / pane el handle (in state.els).
const TAB_ELS = {
  home: 'tabHome', ac: 'tabAc', energy: 'tabEnergy', plugs: 'tabPlugs', security: 'tabSecurity',
};
const PANE_ELS = {
  home: 'paneHome', ac: 'paneAc', energy: 'paneEnergy', plugs: 'panePlugs', security: 'paneSecurity',
};
let onChange = function () {};

export function onTabChange(fn) {
  onChange = fn;
}

export function setTab(tab) {
  if (!TABS.includes(tab)) tab = 'home';
  state.tab = tab;
  TABS.forEach(function (name) {
    const tabEl = els[TAB_ELS[name]];
    const paneEl = els[PANE_ELS[name]];
    const active = name === tab;
    tabEl.classList.toggle('active', active);
    tabEl.setAttribute('aria-selected', active ? 'true' : 'false');
    tabEl.tabIndex = active ? 0 : -1;
    paneEl.hidden = !active;
  });
  const nav = els.tabHome.closest('.tabs');
  if (nav) nav.dataset.activeTab = tab;
  try { localStorage.setItem(TAB_KEY, tab); } catch (_) { /* private mode */ }
  onChange(tab);
}

export function wireTabs() {
  TABS.forEach(function (name) {
    els[TAB_ELS[name]].addEventListener('click', function () { setTab(name); });
  });
  wireBottomTabsPin();
}

// Keep the floating mobile tab bar glued to the *visual* viewport bottom. iOS
// Safari positions `position: fixed` against the layout viewport and only
// re-snaps fixed elements once a scroll / address-bar transition settles, so
// the bar visibly drifts a few px first. Translating it by the visual↔layout
// bottom delta tracks the address bar in real time. No-op when the two
// viewports coincide (delta 0) — desktop, the e2e projections, a settled
// phone — so the desktop nav is never moved. (Ported from app-launcher.)
function pinBottomTabs() {
  const nav = els.tabHome && els.tabHome.closest('.tabs');
  if (!nav) return;
  const vp = window.visualViewport;
  if (!vp) return;
  const layoutH = document.documentElement.clientHeight;
  const delta = Math.round(layoutH - (vp.offsetTop + vp.height));
  nav.style.transform = delta > 0 ? 'translateY(' + -delta + 'px)' : '';
}

function wireBottomTabsPin() {
  if (!window.visualViewport) return;
  pinBottomTabs();
  window.visualViewport.addEventListener('resize', pinBottomTabs);
  window.visualViewport.addEventListener('scroll', pinBottomTabs);
  window.addEventListener('scroll', pinBottomTabs, { passive: true });
}

export function initialTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (TABS.includes(stored)) return stored;
  } catch (_) { /* private mode */ }
  return 'home';
}
