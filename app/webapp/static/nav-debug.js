/* Persistent on-device diagnostic for the iOS modal/nav-pin bugs (#300).
 *
 * Superseded two earlier attempts: a throwaway `?navdebug=1` query-param
 * overlay lost its flag the moment the page was "Added to Home Screen", and
 * the first on-screen panel that replaced it grew tall enough during a real
 * test session to cover the whole viewport and block interaction. This
 * version instead:
 *   - toggles via a UI button (the gauge icon next to the theme toggle) and
 *     persists the on/off state to localStorage, exactly like the theme and
 *     tab selection, so it survives a PWA relaunch from the home screen.
 *   - posts each event straight to the server (POST /api/nav-debug, which
 *     appends to the gitignored `webapp/nav_debug.log`) instead of rendering
 *     anything on screen, so a reproduction session can be read back
 *     directly from disk — no screenshot, no on-screen clutter, and no risk
 *     of the interesting part having already scrolled out of a panel. */

'use strict';

import { api } from './api.js';

export const NAV_DEBUG_KEY = 'home-automation.nav-debug';

let enabled = false;
let sabProbe = null;
let vhProbes = null;   // {lvh, svh, dvh} — 0-width fixed divs sized in each unit

function isStandalonePwa() {
  return window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;
}

function fmt(n) {
  return typeof n === 'number' ? n.toFixed(1) : n;
}

function snapshot(nav) {
  const vv = window.visualViewport;
  const app = document.querySelector('.app');
  const s = {
    scrollY: window.scrollY,
    appScrollTop: app ? fmt(app.scrollTop) : null,  // the scroller in the standalone shell (#303)
    innerHeight: window.innerHeight,
    // Viewport forensics (#303 round 2): round 1's dead band came from the
    // fixed-positioning viewport itself shrinking, which no single number
    // above could prove. Comparing these pins down WHICH viewport moved:
    // clientHeight = the ICB, screenHeight = the physical screen,
    // appRectBottom = where the fixed-inset scroller's box actually ends,
    // safeAreaBottom = what env(safe-area-inset-bottom) resolved to.
    clientHeight: document.documentElement.clientHeight,
    screenHeight: window.screen ? window.screen.height : null,
    safeAreaBottom: sabProbe ? getComputedStyle(sabProbe).top : null,
    appRectBottom: app ? fmt(app.getBoundingClientRect().bottom) : null,
    // Round-3 forensics: whether the standalone layout viewport is expanded
    // (lvh ≈ screen) or contracted (dvh ≈ clientHeight) at this instant —
    // the round-2 log proved fixed anchoring follows this, not the screen.
    lvh: vhProbes ? fmt(vhProbes.lvh.getBoundingClientRect().height) : null,
    svh: vhProbes ? fmt(vhProbes.svh.getBoundingClientRect().height) : null,
    dvh: vhProbes ? fmt(vhProbes.dvh.getBoundingClientRect().height) : null,
    vvHeight: vv ? fmt(vv.height) : null,
    vvOffsetTop: vv ? fmt(vv.offsetTop) : null,
    bodyPos: document.body.style.position || null,
    standalone: isStandalonePwa(),
    dialogOpen: !!document.querySelector('dialog[open]'),
  };
  if (nav) {
    const r = nav.getBoundingClientRect();
    s.navPosition = getComputedStyle(nav).position;
    s.navVisibility = getComputedStyle(nav).visibility;
    s.navRectTop = fmt(r.top);
    s.navRectBottom = fmt(r.bottom);
    s.navTransform = nav.style.transform || null;
  }
  return s;
}

// Fire-and-forget: a debug sink must never slow down or break the app it's
// diagnosing. Errors (offline, auth hiccup) are swallowed — a gap in the log
// just means "reproduce again," never a broken UI.
function record(event) {
  if (!enabled) return;
  const nav = document.querySelector('.tabs');
  const entry = Object.assign(
    { ts: Date.now(), t: performance.now().toFixed(0), event: event },
    snapshot(nav)
  );
  api('/api/nav-debug', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(entry),
    timeoutMs: 5000,
  }).catch(function () { /* best-effort — see comment above */ });
}

export function isNavDebugEnabled() {
  return enabled;
}

export function setNavDebugEnabled(next) {
  enabled = next;
  try { localStorage.setItem(NAV_DEBUG_KEY, enabled ? '1' : '0'); } catch (_) { /* private mode */ }
  if (enabled) record('debug-on');
}

// Records a transform change or other nav-pin event; called from tabs.js so
// the log captures *why* the bar moved, not just periodic snapshots.
export function recordNavEvent(event) {
  record(event);
}

export function installNavDebug() {
  try { enabled = localStorage.getItem(NAV_DEBUG_KEY) === '1'; } catch (_) { enabled = false; }

  // Zero-size fixed probe whose `top` is env(safe-area-inset-bottom): reading
  // it back from getComputedStyle is the only way to observe the value the
  // engine actually resolved for env() (#303 round-2 viewport forensics).
  sabProbe = document.createElement('div');
  sabProbe.style.cssText = 'position:fixed;top:env(safe-area-inset-bottom,0px);left:-9999px;width:0;height:0;pointer-events:none;visibility:hidden;';
  document.body.appendChild(sabProbe);

  // 0-width probes sized in each viewport unit (#303 round 3): their measured
  // heights expose lvh/svh/dvh, which no JS API reports directly.
  function mkVhProbe(unit) {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;top:0;left:-9999px;width:0;height:100' + unit + ';pointer-events:none;visibility:hidden;';
    document.body.appendChild(d);
    return d;
  }
  vhProbes = { lvh: mkVhProbe('lvh'), svh: mkVhProbe('svh'), dvh: mkVhProbe('dvh') };

  if (enabled) record('debug-on (restored on load)');

  document.addEventListener('scroll-lock:engaged', function () { record('scroll-lock:engaged'); });
  document.addEventListener('scroll-lock:released', function () { record('scroll-lock:released'); });

  // Keyboard show/hide is the user's own lead suspect (#300) — an editable
  // field grabbing focus inside a dialog is exactly what brings the iOS
  // keyboard up and shrinks the visual viewport far more than a toolbar ever
  // does.
  document.addEventListener('focusin', function (e) {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) {
      record('focusin:' + t.tagName);
    }
  });
  document.addEventListener('focusout', function (e) {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) {
      record('focusout:' + t.tagName);
    }
  });

  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', function () { record('vv:resize'); });
  }

  // Plain document scroll (#300 round 2): every earlier event showed
  // `navPosition: fixed` and a correct `navRect` at every sampled moment,
  // even right through a keyboard show/hide — yet the bar was seen visibly
  // tracking scroll, and none of the events above fire during a plain scroll.
  // This turned out to be the key sample: it caught `navRect` shifting in
  // lockstep with `visualViewport.offsetTop` during ordinary momentum
  // scrolling (no dialog/keyboard involved), which is what actually explains
  // the drift — see the postmortem in tabs.js's `pinNavToVisualViewport`.
  let scrollScheduled = false;
  window.addEventListener('scroll', function () {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(function () { scrollScheduled = false; record('window:scroll'); });
  }, { passive: true });

  // Standalone shell (#303): scrolling moved from the document to the `.app`
  // element scroller, and element scroll events don't bubble to window —
  // without this listener a standalone repro session would log no scroll
  // samples at all, which is exactly the moment the #300 drift showed up.
  const appScroller = document.querySelector('.app');
  if (appScroller) {
    let appScrollScheduled = false;
    appScroller.addEventListener('scroll', function () {
      if (appScrollScheduled) return;
      appScrollScheduled = true;
      requestAnimationFrame(function () { appScrollScheduled = false; record('app:scroll'); });
    }, { passive: true });
  }

  const obs = new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      if (m.target.nodeType === 1 && m.target.tagName === 'DIALOG') {
        record('dialog:' + m.attributeName + '=' + (m.target.hasAttribute(m.attributeName) ? (m.target.getAttribute(m.attributeName) || 'true') : 'removed'));
      }
    });
  });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['open'], subtree: true });
}
