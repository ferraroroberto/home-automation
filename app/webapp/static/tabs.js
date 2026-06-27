/* Tab switcher: Home | AC | Energy | Plugs | Light | Net | Alarm.
 *
 * Mirrors app-launcher's nav.tabs pattern: .tab buttons get .active, .pane
 * sections toggle via [hidden]. The chosen tab is written to localStorage and
 * restored on launch; the #232 body-level nav keeps short tabs from capturing the
 * fixed bar. main.js registers an onChange hook so the Energy tab can spin up its
 * charts + faster polling. */

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
  const scroller = document.querySelector('.app');
  if (scroller) scroller.scrollTop = 0;
  window.scrollTo(0, 0);
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

function isStandalonePwa() {
  return window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;
}

function editableFocused() {
  const a = document.activeElement;
  if (!a) return false;
  const tag = a.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || a.isContentEditable;
}

/* Keep the floating bottom-tab pill pinned to the bottom on mobile — CSS-first,
 * with a minimal browser-only transform fallback (issue #229).
 *
 * Hard-won lesson: the JS transform is the enemy in a standalone PWA. styles.css
 * positions the bar `fixed; bottom: …`, which is correct on its own in an installed
 * PWA (no browser chrome). The VisualViewport transform only ever existed to chase
 * Safari's collapsing *browser* toolbar (#179) — and in standalone every flavour of
 * that math (compute-the-offset AND measure-the-rect) eventually strands the bar UP
 * and won't bring it back, because iOS's layout and rendered geometry disagree there.
 * So the rule is now blunt: in a standalone PWA we NEVER translate. CSS owns the
 * position, and the nav is a body-level sibling rather than a descendant of the
 * content wrapper so iOS anchors it to the viewport rather than to short-tab
 * content.
 *
 * The transform path survives ONLY for a real browser tab, where the toolbar
 * genuinely collapses: there we translate the bar up by the hidden slice, clamped to
 * a toolbar's height and suppressed while the keyboard is up. A periodic watchdog
 * keeps it self-correcting. None of that runs in the PWA.
 *
 * Gated to the coarse-pointer / narrow floating-bar mode (desktop renders .tabs as a
 * sticky top control, where a transform would be wrong) and feature-gated on
 * window.visualViewport (older browsers keep the CSS-only behaviour — no error).
 *
 * Mirror of project-scaffolding's _vendored/nav/nav-tabs.js (issue #92) until this
 * app adopts that vendored component — propagate up to the master (#184). */
function pinNavToVisualViewport(nav) {
  const vv = window.visualViewport;
  if (!vv) return;
  const mq = window.matchMedia('(pointer: coarse) and (max-width: 520px)');

  let rafPending = false;

  // Largest slice we'll pin against in a browser tab — Safari's toolbar (~44–90px).
  // A bigger gap is the soft keyboard / a picker, which we must not chase.
  const MAX_PIN_PX = 160;

  function apply() {
    if (!mq.matches) {
      // Desktop / wide: CSS owns the bar. Drop any transform we may have left on.
      if (nav.style.transform !== '') nav.style.transform = '';
      return;
    }
    // Hidden behind a modal/overlay — re-pin the instant it reappears.
    if (getComputedStyle(nav).visibility === 'hidden') return;
    // Standalone PWA → never translate (CSS owns it). Browser tab → compensate for
    // the collapsing toolbar, but not while a field is focused (keyboard up) and
    // never by more than a toolbar's worth.
    let desired = '';
    if (!isStandalonePwa() && !editableFocused()) {
      const hidden = window.innerHeight - vv.height - vv.offsetTop;
      if (hidden > 1 && hidden <= MAX_PIN_PX) desired = 'translateY(' + -hidden + 'px)';
    }
    if (nav.style.transform !== desired) nav.style.transform = desired;
  }

  // Coalesce burst events (vv scroll/resize fire rapidly) into one paint.
  function schedule() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () { rafPending = false; apply(); });
  }

  vv.addEventListener('resize', schedule);
  vv.addEventListener('scroll', schedule);
  if (mq.addEventListener) mq.addEventListener('change', apply);

  // Reliable modal-close re-pin: watch the `open`/`hidden` attributes (the signal
  // scroll-lock.js trusts) rather than the `close` event that doesn't reliably
  // fire. Double-rAF so we recompute AFTER scroll-lock has restored the body and
  // layout has settled. Covers <dialog> open/close and the #loginOverlay toggle.
  const obs = new MutationObserver(function () {
    requestAnimationFrame(function () { requestAnimationFrame(apply); });
  });
  obs.observe(document.documentElement, {
    attributes: true, attributeFilter: ['open', 'hidden'], subtree: true,
  });

  // Self-healing watchdog: the ultimate backstop. Even if every event above is
  // missed, this re-derives the resting position and pulls a stranded bar back
  // down within ~400ms. Paused while the page is backgrounded (no layout to fix,
  // and timers are throttled there anyway).
  setInterval(function () { if (!document.hidden) apply(); }, 400);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) apply();        // re-pin immediately on foreground
  });
  // Startup / foreground: run after the first settled layout too, so any stale
  // browser-tab toolbar offset is corrected before the watchdog interval.
  window.addEventListener('load', function () {
    requestAnimationFrame(function () { requestAnimationFrame(apply); });
  });

  apply();
}

// Restore the last tab per the fleet nav contract. The old #229 Home-only
// cold-start workaround is no longer needed because #232 keeps the fixed nav out
// of the scroller that iOS can capture.
export function initialTab() {
  try {
    const saved = localStorage.getItem(TAB_KEY);
    if (TABS.includes(saved)) return saved;
  } catch (_) { /* private mode */ }
  return 'home';
}
