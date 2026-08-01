/* Shared confirm dialog — a styled <dialog> returning a Promise<boolean> (#129).
 *
 * Nothing here is network-specific, but it lived in and was exported from
 * `network.js` (a feature tab) until issue #574, so five unrelated modules —
 * including the *shared* `dense-editor.js` primitive — had to import a feature
 * tab just to ask a yes/no question. That dependency direction was inverted;
 * this module is the neutral home every caller can point at.
 *
 * Callers use it instead of a native `confirm()`, which is the one
 * design-system-breaking element this app would otherwise have.
 */

'use strict';

import { els } from './state.js';
import { closeDialog, openDialog } from './dialog.js';

let confirmResolver = null;

function closeConfirm(result) {
  closeDialog(els.confirmDialog);
  const resolve = confirmResolver;
  confirmResolver = null;
  if (resolve) resolve(result);
}

export function confirmAction(opts) {
  els.confirmTitle.textContent = (opts && opts.title) || 'Confirm';
  els.confirmMessage.textContent = (opts && opts.message) || '';
  els.confirmOk.textContent = (opts && opts.okLabel) || 'Confirm';
  els.confirmOk.classList.toggle('is-danger', !!(opts && opts.danger));
  return new Promise(function (resolve) {
    confirmResolver = resolve;
    openDialog(els.confirmDialog);
  });
}

export function wireConfirmDialog() {
  if (!els.confirmDialog) return;
  els.confirmClose.addEventListener('click', function () { closeConfirm(false); });
  els.confirmCancel.addEventListener('click', function () { closeConfirm(false); });
  els.confirmOk.addEventListener('click', function () { closeConfirm(true); });
  els.confirmDialog.addEventListener('click', function (ev) {
    if (ev.target === els.confirmDialog) closeConfirm(false);  // backdrop click
  });
  // Esc fires the native 'cancel' event — resolve false and let it close.
  els.confirmDialog.addEventListener('cancel', function () { closeConfirm(false); });
}
