/* Shared dense-collection staged-dialog editor.
 *
 * `denseListEditor(config)` owns the editor shell shared by
 * security-schedules.js / security-scene.js / security-override.js /
 * presence-places.js: the `editorIndex` / `editor<X>Id` / `editorReturnFocus`
 * / `staged<X>` state, `open<X>Editor` / `close<X>Editor`,
 * `restoreEditorFocus()`, an optimistic save-with-rollback PUT, and the
 * add/close/backdrop-click/save/delete-confirm wiring. Each module keeps
 * only its entry shape (defaults/normalize), its dialog-field
 * populate/collect, and any extra field wiring.
 *
 * Config contract — elements (from state.js's `els`): `dialog`, `addButton`,
 * `closeButton`, `saveButton`, `deleteButton`, `titleEl`, `listEl`, `focusEl`;
 * copy: `titles {add, edit}`, `deleteConfirm {title, message}`,
 * `toasts {saved, failed}` (`saved` is a string, or a function
 * `(afterSaveResult) => string | Promise<string>` resolved after `afterSave`
 * — for a confirmation that needs a value only known post-persist, e.g. a
 * recomputed derived total); behavior: `rowIdAttr` (the
 * summary-row data attribute carrying the entry id), `defaults()`,
 * `getEntries()`, `setEntries(list)`, `normalize(entries)`, `render()`,
 * `populate(staged)`, `collect(staged)` (return `false` to abort the save,
 * e.g. failed validation); persistence: `endpoint`, `bodyKey` (JSON key in
 * both the PUT payload and the response); optional: `stage(source)` (custom
 * staged clone — default shallow spread), `afterOpen(staged)` (post-open
 * async work), `payloadEntries(entries)` (filter what is PUT without
 * touching the staged list), `afterSave(entries)` (awaited after the PUT
 * succeeds and before the saved toast, so a function-form `toasts.saved` can
 * consume its resolved value; `render()` already ran on the optimistic swap,
 * so `afterSave` is for derived-view work, not the row list itself),
 * `requireLoad` (the list is read from the server and each save PUTs the
 * whole list, so a save built on a list that failed to load would erase the
 * stored one: Add is disabled and `save()` refuses until the module reports a
 * successful read through `setLoaded(true)`, and again after a failed one).
 *
 * Returns `{open, close, wire, save, setLoaded, staged}` — `staged` is a live
 * getter so a module's own field listeners can mutate the staged entry in
 * place.
 */

'use strict';

import { toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { confirmAction } from './confirm.js';
import { sheet } from './sheet.js';

export function denseListEditor(config) {
  let editorIndex = null;
  let editorEntryId = null;
  let staged = null;
  let loaded = !config.requireLoad;
  if (config.addButton) config.addButton.disabled = !loaded;

  // The staged sheet shell (sheet.js): ×, Esc and the backdrop discard,
  // and focus goes back to the opener, else the edited row, else Add.
  const shell = sheet(config.dialog, {
    model: 'staged',
    closeButton: config.closeButton,
    fallbackFocus: function () {
      if (editorEntryId) {
        const row = config.listEl.querySelector(
          '[' + config.rowIdAttr + '="' + CSS.escape(editorEntryId) + '"]'
        );
        if (row) return row.querySelector('.action-row-main');
      }
      return config.addButton;
    },
    onClose: function () {
      editorIndex = null;
      editorEntryId = null;
      staged = null;
    },
  });

  function open(index, trigger) {
    editorIndex = index;
    const source = index == null ? config.defaults() : config.getEntries()[index];
    staged = config.stage ? config.stage(source) : { ...source };
    editorEntryId = staged.id;
    config.titleEl.textContent = index == null ? config.titles.add : config.titles.edit;
    config.populate(staged);
    config.deleteButton.hidden = index == null;
    shell.open(trigger);
    config.focusEl.focus();
    if (config.afterOpen) config.afterOpen(staged);
  }

  function close() {
    shell.close();
  }

  function save(entries) {
    if (!loaded) return Promise.resolve(false);
    return saveEntries(config, entries);
  }

  function setLoaded(value) {
    loaded = value;
    if (config.addButton) config.addButton.disabled = !loaded;
  }

  async function onSave() {
    if (!staged) return;
    if (config.collect(staged) === false) return;
    const proposed = config.getEntries().slice();
    if (editorIndex == null) proposed.push(staged);
    else proposed[editorIndex] = staged;
    config.saveButton.disabled = true;
    const saved = await save(proposed);
    config.saveButton.disabled = false;
    if (saved) close();
  }

  async function onDelete() {
    if (editorIndex == null) return;
    const ok = await confirmAction({
      title: config.deleteConfirm.title,
      message: config.deleteConfirm.message,
      okLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const removeIndex = editorIndex;
    const proposed = config.getEntries().filter(function (_entry, idx) {
      return idx !== removeIndex;
    });
    if (await save(proposed)) close();
  }

  function wire() {
    config.addButton.addEventListener('click', function () {
      open(null, config.addButton);
    });
    config.saveButton.addEventListener('click', onSave);
    config.deleteButton.addEventListener('click', onDelete);
  }

  return {
    open: open,
    close: close,
    wire: wire,
    save: save,
    setLoaded: setLoaded,
    get staged() { return staged; },
  };
}

// Optimistic update: swap the list, render, PUT — roll back on failure.
async function saveEntries(config, entries) {
  const previous = config.getEntries();
  config.setEntries(config.normalize(entries));
  config.render();
  const current = config.getEntries();
  const payload = {};
  payload[config.bodyKey] = config.payloadEntries ? config.payloadEntries(current) : current;
  try {
    const body = await jsonApi(config.endpoint, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    config.setEntries((body && body[config.bodyKey]) || []);
    config.render();
    const afterResult = config.afterSave ? await config.afterSave(config.getEntries()) : undefined;
    const savedText = typeof config.toasts.saved === 'function'
      ? await config.toasts.saved(afterResult)
      : config.toasts.saved;
    toast(savedText, 'success');
    return true;
  } catch (exc) {
    config.setEntries(previous);
    config.render();
    if (!isAuthRequired(exc)) {
      toast(config.toasts.failed, 'error');
    }
    return false;
  }
}
