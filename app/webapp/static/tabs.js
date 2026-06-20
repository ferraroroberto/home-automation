/* Tab switcher: Home | AC | Energy | Plugs | Security.
 *
 * Mirrors app-launcher's nav.tabs pattern: .tab buttons get .active, .pane
 * sections toggle via [hidden]. The chosen tab is remembered in localStorage
 * so an installed PWA reopens where you left it. main.js registers an
 * onChange hook so the Energy tab can spin up its charts + faster polling. */

'use strict';

import { els, state, TAB_KEY } from './state.js';

const TABS = ['home', 'ac', 'energy', 'plugs', 'security'];
let onChange = function () {};

export function onTabChange(fn) {
  onChange = fn;
}

export function setTab(tab) {
  if (!TABS.includes(tab)) tab = 'home';
  state.tab = tab;
  els.tabHome.classList.toggle('active', tab === 'home');
  els.tabAc.classList.toggle('active', tab === 'ac');
  els.tabEnergy.classList.toggle('active', tab === 'energy');
  els.tabPlugs.classList.toggle('active', tab === 'plugs');
  els.tabSecurity.classList.toggle('active', tab === 'security');
  els.paneHome.hidden = tab !== 'home';
  els.paneAc.hidden = tab !== 'ac';
  els.paneEnergy.hidden = tab !== 'energy';
  els.panePlugs.hidden = tab !== 'plugs';
  els.paneSecurity.hidden = tab !== 'security';
  try { localStorage.setItem(TAB_KEY, tab); } catch (_) { /* private mode */ }
  onChange(tab);
}

export function wireTabs() {
  els.tabHome.addEventListener('click', function () { setTab('home'); });
  els.tabAc.addEventListener('click', function () { setTab('ac'); });
  els.tabEnergy.addEventListener('click', function () { setTab('energy'); });
  els.tabPlugs.addEventListener('click', function () { setTab('plugs'); });
  els.tabSecurity.addEventListener('click', function () { setTab('security'); });
}

export function initialTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (TABS.includes(stored)) return stored;
  } catch (_) { /* private mode */ }
  return 'home';
}
