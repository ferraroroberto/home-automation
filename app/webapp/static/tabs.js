/* Tab switcher: Home | AC | Energy | Plugs | Light | Net | Alarm.
 *
 * Mirrors app-launcher's nav.tabs pattern: .tab buttons get .active, .pane
 * sections toggle via [hidden]. The chosen tab is remembered in localStorage
 * so an installed PWA reopens where you left it. main.js registers an
 * onChange hook so the Energy tab can spin up its charts + faster polling. */

'use strict';

import { els, state, TAB_KEY } from './state.js';

const TABS = ['home', 'ac', 'energy', 'plugs', 'lights', 'network', 'security'];
// tab name → button el handle / pane el handle (in state.els).
const TAB_ELS = {
  home: 'tabHome', ac: 'tabAc', energy: 'tabEnergy', plugs: 'tabPlugs', lights: 'tabLights', network: 'tabNetwork', security: 'tabSecurity',
};
const PANE_ELS = {
  home: 'paneHome', ac: 'paneAc', energy: 'paneEnergy', plugs: 'panePlugs', lights: 'paneLights', network: 'paneNetwork', security: 'paneSecurity',
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
  const nav = els.tabHome.closest('.tabs');
  if (nav) pinNavToVisualViewport(nav);
}

/* Keep the floating bottom-tab pill pinned to the *visual* viewport on mobile.
 *
 * styles.css positions the mobile bar `fixed; bottom: …` against the *layout*
 * viewport. iOS Safari's dynamic bottom toolbar collapses on scroll-down and
 * re-expands on scroll-up, resizing the layout viewport, which drags a fixed
 * bottom-anchored element up then down — the bar rides loose instead of staying
 * locked (issue #179). The VisualViewport API reports the actually-visible rect;
 * we translate the bar up by the slice of layout viewport hidden below it so it
 * rides the visible bottom edge.
 *
 * Gated to the coarse-pointer / narrow floating-bar mode (desktop renders .tabs
 * as a sticky top control, where a transform would be wrong) and feature-gated
 * on window.visualViewport (older browsers keep the CSS-only behaviour — no
 * error). Self-correcting: the transform is cleared whenever the media query
 * stops matching or nothing is hidden.
 *
 * Mirror of project-scaffolding's _vendored/nav/nav-tabs.js (issue #92) until
 * this app adopts that vendored component. */
function pinNavToVisualViewport(nav) {
  const vv = window.visualViewport;
  if (!vv) return;
  const mq = window.matchMedia('(pointer: coarse) and (max-width: 520px)');

  function update() {
    if (!mq.matches) { nav.style.transform = ''; return; }
    // Height of the layout viewport currently hidden below the visual viewport
    // (Safari's collapsing toolbar / any off-screen slice). Pull the bar up by
    // exactly that so its CSS `bottom` inset is measured from the *visible* edge.
    const hidden = window.innerHeight - vv.height - vv.offsetTop;
    nav.style.transform = hidden > 1 ? 'translateY(' + -hidden + 'px)' : '';
  }

  vv.addEventListener('resize', update);
  vv.addEventListener('scroll', update);
  if (mq.addEventListener) mq.addEventListener('change', update);
  // Recompute when any <dialog> closes. 'close' doesn't bubble, so capture it on
  // document — the bar is hidden (visibility) while a modal is open, and this
  // re-pins it to the visible bottom edge the instant it reappears (#205).
  document.addEventListener('close', update, true);
  update();
}

export function initialTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (TABS.includes(stored)) return stored;
  } catch (_) { /* private mode */ }
  return 'home';
}
