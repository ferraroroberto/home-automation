/* Tab switcher: Home | AC | Energy | Plugs | Light | Net | Alarm.
 *
 * Mirrors app-launcher's nav.tabs pattern: .tab buttons get .active, .pane
 * sections toggle via [hidden]. The chosen tab is written to localStorage and
 * restored on launch; the #232 body-level nav keeps short tabs from capturing the
 * fixed bar. main.js registers an onChange hook so the Energy tab can spin up its
 * charts + faster polling. */

'use strict';

import { els, state, TAB_KEY } from './state.js';
import { recordNavEvent } from './nav-debug.js';

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
  // .app is the scroller in the standalone shell (#303); the window is the
  // scroller everywhere else. Resetting both covers both modes.
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
 * #300 tried counter-translating standalone by `+visualViewport.offsetTop` after
 * an on-device log showed the bar drifting by exactly that much post-keyboard.
 * A second on-device log proved that fix actively harmful: `offsetTop` isn't a
 * stuck post-keyboard residual, it swings continuously during ordinary momentum
 * scrolling in this standalone PWA (no dialog or keyboard involved). A
 * requestAnimationFrame-scheduled correction is structurally one frame behind a
 * fast-changing live value, so it overshoots — and since the bar only has ~21px
 * of clearance below it, correcting for the ~68px swings actually observed pushed
 * the whole bar off the bottom of the screen. A drifting-but-visible bar beats a
 * bar that vanishes, so standalone reverts to never translating. If this is
 * revisited, the fix has to prevent `offsetTop` from moving in the first place
 * (e.g. taming the momentum-scroll bounce itself) rather than chasing it after
 * the fact.
 *
 * #303 does exactly that, in CSS: in standalone all real scrolling moves into
 * `.app` as a fixed-inset element scroller, whose bounce doesn't move the
 * visual viewport — so the native bounce that moved `offsetTop` loses its
 * entry point and the fixed bar — still a body-level SIBLING of the scroller,
 * per #232's capture lesson — stays anchored by construction. The document
 * itself stays *technically* 1px-scrollable via an untouchable spacer, because
 * an unscrollable standalone document contracts the fixed-positioning viewport
 * by the top inset (~59pt measured) and strands everything fixed above the
 * physical bottom — the #303 round-1/2 dead band; see the "#303 shell" comment
 * in styles.css. Standalone still NEVER translates — that rule is unchanged.
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
      if (nav.style.transform !== '') { recordNavEvent('apply:clear(desktop)'); nav.style.transform = ''; }
      return;
    }
    // Hidden behind a modal/overlay — re-pin the instant it reappears.
    if (getComputedStyle(nav).visibility === 'hidden') return;
    // Standalone PWA → never translate (CSS owns it; see the #300 postmortem
    // above). Browser tab → compensate for the collapsing toolbar, but not
    // while a field is focused (keyboard up) and never by more than a
    // toolbar's worth.
    let desired = '';
    if (!isStandalonePwa() && !editableFocused()) {
      const hidden = window.innerHeight - vv.height - vv.offsetTop;
      if (hidden > 1 && hidden <= MAX_PIN_PX) desired = 'translateY(' + -hidden + 'px)';
    }
    if (nav.style.transform !== desired) {
      recordNavEvent('apply:setTransform ' + (nav.style.transform || '(none)') + ' -> ' + (desired || '(none)'));
      nav.style.transform = desired;
    }
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

  // <dialog> open/close re-pin (#300): react to scroll-lock.js's own explicit
  // engaged/released signals instead of an independent MutationObserver racing
  // the same `open` attribute mutation. Two observers guessing at ordering off
  // one DOM change is what let a stale translateY survive into the frame where
  // CSS un-hides the bar again — clearing the transform up front (on `engaged`,
  // before the bar is even hidden) and only recomputing it once scroll-lock
  // confirms the body restore is done (`released`) removes the race entirely.
  document.addEventListener('scroll-lock:engaged', function () {
    if (nav.style.transform !== '') {
      recordNavEvent('engaged:clear ' + nav.style.transform + ' -> (none)');
      nav.style.transform = '';
    }
  });
  document.addEventListener('scroll-lock:released', function () {
    requestAnimationFrame(function () { requestAnimationFrame(apply); });
  });

  // #loginOverlay toggle re-pin: it isn't a <dialog>, so it never fires the
  // scroll-lock events above. Watches `hidden` only (not `open`, which is the
  // <dialog> signal now owned by the events above) so it can't double-fire on
  // the same mutation. Double-rAF so we recompute after layout settles.
  const obs = new MutationObserver(function () {
    requestAnimationFrame(function () { requestAnimationFrame(apply); });
  });
  obs.observe(document.documentElement, {
    attributes: true, attributeFilter: ['hidden'], subtree: true,
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
