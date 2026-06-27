/* iOS-proof background scroll lock for open <dialog>s (#214).
 *
 * Two independent problems this solves:
 *   1. iOS Safari ignores `overflow:hidden` on the body for *touch* scrolling,
 *      so the document keeps scrolling behind a modal, dragging the page and the
 *      fixed dialog up off the top of the screen. The only reliable cross-browser
 *      lock is to pin the body with `position: fixed` at the negative current
 *      scroll offset while a modal is open, then restore the exact scroll
 *      position on close.
 *   2. The `close` *event* is not a dependable signal here (it did not fire on
 *      `dialog.close()` in testing), and Esc-dismiss never routes through the
 *      app's close handlers at all. So instead of listening for events or
 *      patching showModal/close, we watch the one thing that always changes — the
 *      `open` *attribute* on the <dialog> — with a MutationObserver, and re-derive
 *      the lock from live DOM truth (`dialog[open]`) on every change. It can't
 *      desync or leak the way a ref-count or a missed event would. */

'use strict';

let locked = false;
let savedScrollY = 0;

function engage() {
  if (locked) return;                       // keep the first save across stacked dialogs
  savedScrollY = window.scrollY || window.pageYOffset || 0;
  const b = document.body;
  b.style.position = 'fixed';
  b.style.top = -savedScrollY + 'px';
  b.style.left = '0';
  b.style.right = '0';
  b.style.width = '100%';
  locked = true;
}

function release() {
  if (!locked) return;
  const b = document.body;
  b.style.position = '';
  b.style.top = '';
  b.style.left = '';
  b.style.right = '';
  b.style.width = '';
  locked = false;
  window.scrollTo(0, savedScrollY);         // restore the offset the negative top stood in for
}

// Single source of truth: locked iff a dialog is open, released otherwise.
function sync() {
  if (document.querySelector('dialog[open]')) engage();
  else release();
}

export function installDialogScrollLock() {
  if (window.__scrollLockInstalled) return;
  // showModal() adds the `open` attribute; close()/Esc/form-submit remove it.
  // Observing the attribute (subtree, so every current and future dialog is
  // covered) makes the lock event-independent. A <details open> toggle elsewhere
  // also fires this, but sync() then just re-checks `dialog[open]` — a cheap
  // idempotent no-op when no modal is involved.
  const obs = new MutationObserver(sync);
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['open'], subtree: true });
  window.__scrollLockInstalled = true;
}
