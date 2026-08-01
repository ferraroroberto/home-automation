/* Shared <dialog> open/close with the non-supporting-browser fallback (#571).
 *
 * Every modal in this app — the AC detail, the plug/light/camera/detector/
 * device/Wi-Fi/presence detail modals, the camera live + zoom views, the
 * survey and map pickers, the confirm dialog, and denseListEditor's staged
 * editor — opened and closed with the same two-line feature detect, written
 * out sixteen times each. `showModal()`/`close()` are absent on the oldest
 * WebKit this PWA still has to run on, where toggling the `open` attribute is
 * the (non-modal) fallback; keeping that decision in one place is what stops
 * one modal quietly regressing on an old iPhone.
 */

'use strict';

export function openDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
}

export function closeDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}
