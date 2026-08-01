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
 * `toasts {saved, failed}` (`saved` is a string, or a function
 * `(afterSaveResult) => string | Promise<string>` resolved after `afterSave`
 * — for a confirmation that needs a value only known post-persist, e.g. a
 * recomputed derived total, issue #564); behavior: `rowIdAttr` (the
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
 * so `afterSave` is for derived-view work, not the row list itself).
 *
 * Returns `{open, close, wire, save, staged}` — `staged` is a live getter so
 * a module's own field listeners can mutate the staged entry in place.
 */

'use strict';

import { toast } from './state.js';
import { jsonApi } from './api.js';
import { confirmAction } from './confirm.js';
import { buildToggle } from './toggle.js';
import { icon } from './_vendored/icons/icons.js';
import { closeDialog, openDialog } from './dialog.js';

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
    openDialog(config.dialog);
    config.focusEl.focus();
    if (config.afterOpen) config.afterOpen(staged);
  }

  function close() {
    closeDialog(config.dialog);
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
      const afterResult = config.afterSave ? await config.afterSave(config.getEntries()) : undefined;
      const savedText = typeof config.toasts.saved === 'function'
        ? await config.toasts.saved(afterResult)
        : config.toasts.saved;
      toast(savedText, 'success');
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

/* One summary row in a dense-collection list (issue #571).
 *
 * `security-schedules` / `security-scene` / `security-override` / `reminders`
 * each hand-rolled the same ~30 lines: a `.list-row.automation-summary-row`
 * holding a full-width summary button (title + optional meta + chevron) that
 * opens the editor, plus a trailing enable/done toggle. This owns that
 * scaffolding; each caller supplies only its own copy and handlers.
 *
 * `opts`:
 *   id / idAttr  entry id and the camelCase `dataset` key it lands on
 *   rowClass     extra class on the row (e.g. reminders' `reminder-row is-done`)
 *   title        the row's headline text
 *   meta         optional second line; omitted when falsy
 *   openLabel    aria-label on the summary button
 *   onOpen(btn)  click handler, passed the button so it can be refocused
 *   toggleName   `buildToggle`'s class name
 *   toggleOn     the toggle's current state
 *   toggleLabel  aria-label on the toggle
 *   onToggle(on) toggle handler
 */
export function renderSummaryRow(opts) {
  const row = document.createElement('div');
  row.className = 'list-row automation-summary-row' + (opts.rowClass ? ' ' + opts.rowClass : '');
  if (opts.idAttr) row.dataset[opts.idAttr] = opts.id;

  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'automation-summary-main';
  main.setAttribute('aria-label', opts.openLabel);

  const copy = document.createElement('span');
  copy.className = 'automation-summary-copy';
  const title = document.createElement('span');
  title.className = 'automation-summary-title';
  title.textContent = opts.title;
  copy.appendChild(title);
  if (opts.meta) {
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = opts.meta;
    copy.appendChild(meta);
  }
  main.appendChild(copy);
  main.insertAdjacentHTML('beforeend', icon('chevron-right', 'automation-summary-chevron'));
  main.addEventListener('click', function () { opts.onOpen(main); });
  row.appendChild(main);

  const toggle = buildToggle(opts.toggleName, opts.toggleOn, opts.onToggle);
  toggle.setAttribute('aria-label', opts.toggleLabel);
  row.appendChild(toggle);
  return row;
}
