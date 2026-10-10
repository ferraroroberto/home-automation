/* Settings pane rows and their sheets (#886, Step 8/8 of #872).
 *
 * Settings is inset groups (design.md `settings group`): every row names a
 * sheet in `data-settings-sheet`, and that sheet holds the controls its card
 * held before, under the same ids, so the modules that load and save them
 * own them as before. This module only opens and closes the sheets, all on
 * the instant model (sheet.js): a change applies as it is made, Done closes.
 *
 * A sheet whose content is read only when someone looks (the voice
 * catalogue, the sun-position chart) registers `onSettingsSheetOpen`; the
 * hook runs after the dialog is open, so a chart sizes to a real box.
 */

'use strict';

import { sheet } from './sheet.js';

const sheets = {};
const openHooks = {};

function rowFor(id) {
  return document.querySelector('#paneSettings [data-settings-sheet="' + id + '"]');
}

export function onSettingsSheetOpen(id, fn) {
  (openHooks[id] = openHooks[id] || []).push(fn);
}

// `trigger` is where focus returns on close; a link from another tab passes
// nothing, so focus lands on the sheet's own row in Settings.
export function openSettingsSheet(id, trigger) {
  const shell = sheets[id];
  if (!shell) return;
  shell.open(trigger || rowFor(id));
  (openHooks[id] || []).forEach(function (fn) { fn(); });
}

export function wireSettings() {
  document.querySelectorAll('dialog.settings-sheet').forEach(function (dialog) {
    sheets[dialog.id] = sheet(dialog, {
      model: 'instant',
      closeButton: dialog.querySelector('.detail-close'),
      doneButton: dialog.querySelector('[data-sheet-done]'),
    });
  });
  document.querySelectorAll('#paneSettings [data-settings-sheet]').forEach(function (row) {
    row.addEventListener('click', function () { openSettingsSheet(row.dataset.settingsSheet, row); });
  });
}
