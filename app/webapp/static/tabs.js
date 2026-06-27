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

/* Keep the floating bottom-tab pill planted at the bottom of the *visible*
 * viewport on mobile — a SELF-HEALING, MEASUREMENT-driven controller (#229).
 *
 * The long way round. styles.css positions the bar `fixed; bottom: …`. That
 * should pin it to the viewport bottom, but iOS breaks it three different ways:
 * Safari's collapsing toolbar resizes the layout viewport (#179); modal scroll-
 * lock pins the body `position:fixed` (#205/#214/#216); and — the one that kept
 * coming back — in a standalone PWA on a SHORT, non-scrolling page the fixed bar
 * anchors to the *content* bottom, not the screen, until a reflow nudges it
 * (visible at cold-start on the compact Plugs tab). Every earlier fix tried to
 * *compute* the right offset from `innerHeight`/`vv.height`/`vv.offsetTop`; each
 * computation was right for one trigger and wrong (or stale-latched) for the
 * next, so the bar kept stranding and only an app restart fixed it.
 *
 * Stop computing, start MEASURING. We don't model *why* the bar is misplaced —
 * we read where it actually is and push it where it belongs:
 *   target  = bottom of the visible viewport, minus the bar's CSS `bottom` inset
 *   base    = the bar's real rendered bottom with its own transform backed out
 *             (read live from getComputedStyle().transform — never a cached value)
 *   ty      = target − base   → the exact translate that lands it on target
 * At rest ty≈0 (CSS already correct → no transform). Floated up on a short page,
 * ty is positive and pushes it back DOWN. Stale-latched, ty re-derives from the
 * live rect and corrects. Because `base` backs out the *actual* applied transform,
 * the loop converges in one tick and can't oscillate, and it self-heals any
 * displacement no matter how it arose — toolbar, reflow, cold-start, or a strand
 * we didn't author. This is exactly the "force it to the lower part" the bar
 * needed; it's content- and mode-independent, so it works on every tab.
 *
 * Guard: while the soft keyboard is up the visible viewport shrinks ~250–340px,
 * and chasing that would yank the bar up by a keyboard's height (the plugs/AC
 * rename modal auto-focuses its text input). We detect the large shrink and the
 * focused field and simply don't move the bar — it's hidden behind the modal
 * anyway, and we re-measure the instant the keyboard closes.
 *
 * Driven by RELIABLE signals so it corrects promptly, with a WATCHDOG backstop:
 * VisualViewport resize/scroll (rAF-coalesced), a MutationObserver on
 * `dialog[open]`/`#loginOverlay[hidden]` (the dependable signal scroll-lock.js
 * trusts, not the flaky `close` event), window `load`, and a ~400ms interval
 * (paused when the page is hidden). Whatever displaces the bar, the next tick
 * measures it and lands it back — no restart, ever.
 *
 * Gated to the coarse-pointer / narrow floating-bar mode (desktop renders .tabs
 * as a sticky top control, where a transform would be wrong) and feature-gated on
 * window.visualViewport (older browsers keep the CSS-only behaviour — no error).
 *
 * Mirror of project-scaffolding's _vendored/nav/nav-tabs.js (issue #92) until this
 * app adopts that vendored component — propagate this controller up to the master
 * (#184) so the whole fleet inherits the self-healing behaviour. */
function pinNavToVisualViewport(nav) {
  const vv = window.visualViewport;
  if (!vv) return;
  const mq = window.matchMedia('(pointer: coarse) and (max-width: 520px)');

  let rafPending = false;

  // A visible-viewport shrink bigger than this is the soft keyboard / a picker,
  // not a browser toolbar (~44–90px). When it's up we don't move the bar (it's
  // behind the modal) and re-measure once the viewport restores.
  const KEYBOARD_SHRINK_PX = 120;

  function isEditableFocused() {
    const a = document.activeElement;
    if (!a) return false;
    const tag = a.tagName;
    // Anything that raises the iOS keyboard. SELECT opens a picker that also
    // shrinks the viewport, so treat it the same.
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || a.isContentEditable;
  }

  // Current translateY actually applied to the bar, read live from computed style
  // (not a cached "what we wrote") — so backing it out of the measured rect always
  // reflects DOM truth, which is what makes the loop self-healing.
  function currentTranslateY() {
    const t = getComputedStyle(nav).transform;
    if (!t || t === 'none') return 0;
    try { return new DOMMatrixReadOnly(t).m42; } catch (_) { return 0; }
  }

  // Measure where the bar is, push it to where it belongs. See the header comment.
  function apply() {
    if (!mq.matches) {
      // Desktop / wide: CSS owns the bar. Drop any transform we may have left on.
      if (nav.style.transform !== '') nav.style.transform = '';
      return;
    }
    // Hidden behind a modal/overlay — re-measure the instant it reappears.
    if (getComputedStyle(nav).visibility === 'hidden') return;
    // Keyboard / picker up — don't chase the shrunken viewport.
    if (window.innerHeight - vv.height > KEYBOARD_SHRINK_PX || isEditableFocused()) return;

    const cssBottom = parseFloat(getComputedStyle(nav).bottom) || 0;
    const base = nav.getBoundingClientRect().bottom - currentTranslateY();
    const target = vv.offsetTop + vv.height - cssBottom;
    const ty = target - base;
    const desired = Math.abs(ty) > 1 ? 'translateY(' + ty + 'px)' : '';
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
  // Cold-start: the short-page float is wrong until the first layout settles, so
  // correct on load and across the next couple of frames (don't wait for the
  // 400ms watchdog to clear a visible float on the tab the PWA reopened on).
  window.addEventListener('load', function () {
    requestAnimationFrame(function () { requestAnimationFrame(apply); });
  });

  apply();
}

export function initialTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (TABS.includes(stored)) return stored;
  } catch (_) { /* private mode */ }
  return 'home';
}
