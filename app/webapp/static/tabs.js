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

/* Keep the floating bottom-tab pill pinned to the *visual* viewport on mobile —
 * as a SELF-HEALING, closed-loop controller (issue #229).
 *
 * Why a controller and not a one-shot transform: styles.css positions the mobile
 * bar `fixed; bottom: …` against the *layout* viewport. iOS Safari's dynamic
 * bottom toolbar collapses/expands on scroll and resizes the layout viewport,
 * dragging a fixed bottom-anchored element loose (issue #179); modal open/close
 * pins the body `position:fixed` and perturbs the viewport math (#205/#214/#216).
 * The fix is to translate the bar up by the slice of layout viewport hidden below
 * the visual viewport — `hidden = innerHeight - vv.height - vv.offsetTop` — so its
 * CSS `bottom` inset is measured from the *visible* edge.
 *
 * The old design wrote that transform open-loop on a handful of events and trusted
 * them to clear it again. Two ways that stranded the bar UP for good: (1) the
 * transform LATCHES — once any event fired while the math was transiently wrong
 * (mid modal-open, half-collapsed toolbar) the stale translate persisted with
 * nothing to re-derive it; (2) the reset relied on the `close` event, which
 * scroll-lock.js documents as NOT firing dependably on dialog.close()/Esc. Net
 * effect: the bar got stuck up and only an app restart fixed it.
 *
 * THE BIG ONE — standalone PWA has no toolbar, so it needs no transform at all.
 * The whole transform exists to compensate for Safari's collapsing *browser*
 * toolbar. An installed PWA (iOS standalone / `display-mode: standalone`) runs
 * fullscreen with no browser chrome, so `position:fixed; bottom` already pins the
 * bar to the physical bottom and `position:fixed` ignores the soft keyboard (it's
 * laid out against the layout viewport, which the keyboard doesn't shrink). In
 * that mode the VisualViewport math only ever computes phantom offsets — after the
 * keyboard closes iOS leaves `innerHeight`/`vv.height`/`offsetTop` transiently out
 * of sync — and strands the bar UP (the ~140px residual seen on the plugs rename
 * modal, #229). So in standalone we NEVER translate: CSS owns the position and the
 * bar is forced to the bottom "as in the beginning". The transform path is kept
 * only for a real browser tab, where the toolbar genuinely collapses (#179).
 *
 * Closed-loop fix — these properties make displacement self-correcting:
 *   - Re-DERIVE every tick; at rest (and always in standalone) desired is '' so the
 *     transform clears and the bar sits down. State is never trusted, only the live
 *     measurement reconciled against the bar's actual transform.
 *   - Drive re-pins off RELIABLE signals: VisualViewport resize/scroll (the normal
 *     case) PLUS a MutationObserver on `dialog[open]` / `#loginOverlay[hidden]` —
 *     the same dependable attribute signal scroll-lock.js uses — instead of the
 *     flaky `close` event.
 *   - A periodic WATCHDOG (~400ms, paused when the page is hidden) re-derives and
 *     clears any stray transform no matter how it arose. This is the "if it's up,
 *     force it back down" backstop: recovery within one tick, so closing/reopening
 *     the app is never required again.
 *
 * Gated to the coarse-pointer / narrow floating-bar mode (desktop renders .tabs as
 * a sticky top control, where a transform would be wrong) and feature-gated on
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

  // Largest viewport slice we'll ever pin against. Safari's bottom toolbar is
  // ~44–90px; a gap bigger than this means the visual viewport shrank for some
  // OTHER reason — almost always the soft keyboard (~250–340px) raised by a
  // focused text field, e.g. the plugs/AC modal's auto-focused "Display name"
  // input. Pinning the bar up by a keyboard's height is exactly the residual
  // strand reported after #229. Above this cap we leave the bar at its CSS bottom
  // and let it settle once the viewport restores. Generic by construction: we
  // don't need to know *why* the viewport shrank, only that a toolbar can't be
  // this tall.
  const MAX_PIN_PX = 160;

  // Installed PWA: no browser toolbar, so the transform compensation must be off.
  // `navigator.standalone` is the iOS-specific signal; the media query covers the
  // standard (iOS 16.4+ and other platforms). Checked live every tick — a PWA
  // can't change mode mid-session, but reading it is free and keeps apply() pure.
  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches ||
      window.navigator.standalone === true;
  }

  function isEditableFocused() {
    const a = document.activeElement;
    if (!a) return false;
    const tag = a.tagName;
    // Anything that raises the iOS keyboard. SELECT opens a picker that also
    // shrinks the viewport, so treat it the same.
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || a.isContentEditable;
  }

  // The single source of truth for where the bar should sit. Re-derived from the
  // live viewport every call, then reconciled against the bar's *actual* inline
  // transform — never against a cached "what we last wrote". That distinction is
  // the whole self-healing property: a strand we didn't author (a missed event,
  // a stale latch, anything) still gets corrected, because we compare desired to
  // the real DOM state, not to our own memory of it. The equality check only
  // skips a redundant write when the bar is already where it belongs.
  function apply() {
    if (!mq.matches) {
      // Desktop / wide: CSS owns the bar. Drop any transform we may have left on.
      if (nav.style.transform !== '') nav.style.transform = '';
      return;
    }
    // While the bar is hidden behind a modal/overlay the body is position:fixed
    // and the viewport math is transient — leave the transform untouched and
    // re-pin the instant it reappears (the observer + watchdog catch that).
    if (getComputedStyle(nav).visibility === 'hidden') return;
    // Desired transform: at rest it's '' (CSS owns the position); only in a real
    // browser tab with a collapsed toolbar is it a small upward translate that
    // re-pins the bar to the visible bottom edge.
    let desired = '';
    // Never translate in a standalone PWA (no toolbar — CSS already pins it, and
    // the math only strands it). In a browser tab, also don't chase the viewport
    // while a field is focused (keyboard up — a huge, transient change), and never
    // pin by more than a toolbar's worth. Any one of these guards stops the strand.
    if (!isStandalone() && !isEditableFocused()) {
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

  apply();
}

export function initialTab() {
  try {
    const stored = localStorage.getItem(TAB_KEY);
    if (TABS.includes(stored)) return stored;
  } catch (_) { /* private mode */ }
  return 'home';
}
