/* Shared dense-collection staged-dialog editor (issue #452).
 *
 * security-schedules.js / security-scene.js / security-override.js /
 * presence-places.js each hand-rolled the same editor shell: the
 * `editorIndex` / `editor<X>Id` / `editorReturnFocus` / `staged<X>` module
 * vars, `open<X>Editor` / `close<X>Editor`, a byte-identical
 * `restoreEditorFocus()`, an optimistic save-with-rollback PUT, and the
 * add/close/backdrop-click/save/delete-confirm wiring. `denseListEditor(config)`
 * owns that shell; each module keeps only its entry shape (defaults/normalize),
 * its dialog-field populate/collect, and any extra field wiring.
 *
 * Config contract — elements (from state.js's `els`): `dialog`, `addButton`,
 * `closeButton`, `saveButton`, `deleteButton`, `titleEl`, `listEl`, `focusEl`;
 * copy: `titles {add, edit}`, `deleteConfirm {title, message}`,
 * `toasts {saved, failed}`; behavior: `rowIdAttr` (the summary-row data
 * attribute carrying the entry id), `defaults()`, `getEntries()`,
 * `setEntries(list)`, `normalize(entries)`, `render()`, `populate(staged)`,
 * `collect(staged)` (return `false` to abort the save, e.g. failed
 * validation); persistence: `endpoint`, `bodyKey` (JSON key in both the PUT
 * payload and the response); optional: `stage(source)` (custom staged clone —
 * default shallow spread), `afterOpen(staged)` (post-open async work),
 * `payloadEntries(entries)` (filter what is PUT without touching the staged
 * list).
 *
 * Returns `{open, close, wire, save, staged}` — `staged` is a live getter so
 * a module's own field listeners can mutate the staged entry in place.
 */

'use strict';

import { toast } from './state.js';
import { jsonApi } from './api.js';
import { confirmAction } from './network.js';

export function denseListEditor(config) {
  let editorIndex = null;
  let editorEntryId = null;
  let editorReturnFocus = null;
  let staged = null;

  function open(index, trigger) {
    editorIndex = index;
    const source = index == null ? config.defaults() : config.getEntries()[index];
    staged = config.stage ? config.stage(source) : { ...source };
    editorEntryId = staged.id;
    editorReturnFocus = trigger || null;
    config.titleEl.textContent = index == null ? config.titles.add : config.titles.edit;
    config.populate(staged);
    config.deleteButton.hidden = index == null;
    if (typeof config.dialog.showModal === 'function') config.dialog.showModal();
    else config.dialog.setAttribute('open', '');
    config.focusEl.focus();
    if (config.afterOpen) config.afterOpen(staged);
  }

  function close() {
    if (typeof config.dialog.close === 'function') config.dialog.close();
    else config.dialog.removeAttribute('open');
  }

  function restoreFocus() {
    let target = editorReturnFocus && editorReturnFocus.isConnected ? editorReturnFocus : null;
    if (!target && editorEntryId) {
      const row = config.listEl.querySelector(
        '[' + config.rowIdAttr + '="' + CSS.escape(editorEntryId) + '"]'
      );
      if (row) target = row.querySelector('.automation-summary-main');
    }
    if (!target) target = config.addButton;
    editorIndex = null;
    editorEntryId = null;
    editorReturnFocus = null;
    staged = null;
    if (target) requestAnimationFrame(function () { target.focus(); });
  }

  // Optimistic update: swap the list, render, PUT — roll back on failure.
  async function save(entries) {
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
      toast(config.toasts.saved, 'success');
      return true;
    } catch (exc) {
      config.setEntries(previous);
      config.render();
      if (String(exc.message) !== 'auth required') {
        toast(config.toasts.failed, 'error');
      }
      return false;
    }
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
    config.closeButton.addEventListener('click', close);
    config.dialog.addEventListener('click', function (ev) {
      if (ev.target === config.dialog) close();
    });
    config.dialog.addEventListener('close', restoreFocus);
    config.saveButton.addEventListener('click', onSave);
    config.deleteButton.addEventListener('click', onDelete);
  }

  return {
    open: open,
    close: close,
    wire: wire,
    save: save,
    get staged() { return staged; },
  };
}
