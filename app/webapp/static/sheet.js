/* One sheet shell, two save models (#880, Step 2/8 of #872).
 *
 * Every detail sheet in this app is a native <dialog> (design.md `modal`: the
 * editor modal is also the detail sheet). Before this, each module wired the
 * same shell by hand: the × button, a backdrop click, Esc, and (in places)
 * handing focus back to whatever opened it. `sheet(dialog, config)` owns that
 * shell, so every sheet closes the same way and nothing reopens on a stale
 * focus target.
 *
 * The two save models are the ones the design system allows:
 *   'staged'  an editor of a saved item. Save is the only persistence
 *             boundary; ×, Esc and the backdrop all discard (the caller's
 *             `onClose` drops its staged copy). detailModal() and
 *             denseListEditor() build on this.
 *   'instant' a device or settings sheet. Each change applies as it is made,
 *             with a toast; the footer's one primary is Done (`doneButton`),
 *             which only closes.
 * The model is stamped on the dialog as data-save-model, so a test (or a
 * reader in DevTools) can tell which contract a sheet follows.
 *
 * Config: `closeButton` (the ×), `model` ('staged' | 'instant'), optional
 * `doneButton` (instant only), `onClose()` (runs once per close, however it
 * closed), `fallbackFocus()` (an element to focus when the opener is gone,
 * resolved before `onClose`).
 * Returns `{open(trigger), close(), isOpen()}`; `open` remembers `trigger`
 * (or the focused element) and close hands focus back to it.
 */

'use strict';

import { closeDialog, openDialog } from './dialog.js';

export function sheet(dialog, config) {
  const cfg = config || {};
  let returnFocus = null;
  let openNow = false;

  if (dialog) dialog.dataset.saveModel = cfg.model === 'instant' ? 'instant' : 'staged';

  // Runs once per close: native close() fires 'close' (×, Esc, backdrop, a
  // caller's close); the no-showModal fallback has no event, so close() calls
  // it directly there.
  function afterClose() {
    if (!openNow) return;
    openNow = false;
    // The focus target is resolved before onClose, which may clear the state
    // a fallback needs (the dense editor finds the edited row by its id).
    let target = returnFocus && returnFocus.isConnected ? returnFocus : null;
    if (!target && cfg.fallbackFocus) target = cfg.fallbackFocus();
    returnFocus = null;
    if (cfg.onClose) cfg.onClose();
    if (target && typeof target.focus === 'function') {
      requestAnimationFrame(function () { target.focus(); });
    }
  }

  function open(trigger) {
    if (!dialog) return;
    returnFocus = trigger || document.activeElement || null;
    openNow = true;
    openDialog(dialog);
  }

  function close() {
    if (!dialog) return;
    const native = typeof dialog.close === 'function';
    closeDialog(dialog);
    if (!native) afterClose();
  }

  if (dialog) {
    dialog.addEventListener('close', afterClose);
    dialog.addEventListener('click', function (ev) {
      if (ev.target === dialog) close();  // backdrop
    });
  }
  if (cfg.closeButton) cfg.closeButton.addEventListener('click', close);
  if (cfg.doneButton) cfg.doneButton.addEventListener('click', close);

  return {
    open: open,
    close: close,
    isOpen: function () { return openNow; },
  };
}
