/* Shared fixed-DOM single-entity detail-modal save shell (issue #699).
 *
 * plugs.js / circuits.js / security-alarm.js / network-devices.js each
 * hand-roll the same staged-edit shell around a fixed-DOM detail dialog: a
 * `let xStaged` var, a byte-identical `markXDirty()`/`clearXDirty()` pair,
 * and a `saveXDetail()` that builds an array of in-flight PUT/POST ops and
 * awaits them with the same try/Promise.all/toast/catch shell.
 * `detailModal(config)` owns that shell — the staged clone, dirty-button
 * bookkeeping, dialog open/close, and the save try/catch — while each module
 * keeps its own field population (`populate`) and diff-and-request logic
 * (`buildOps`), since those genuinely differ per module's field set.
 *
 * network-wifi.js, cameras.js, presence.js, and units.js were checked
 * against this same audit finding but don't share this shape: they save
 * each field immediately on blur/toggle (no staged-dirty-Save flow) or
 * track dirty per-section rather than as one flag. Forcing them onto this
 * helper would be a UX behavior change, not a mechanical dedup, so they are
 * deliberately left as-is.
 *
 * Config — elements: `dialog`, `saveButton`, `focusEl` (optional); behavior:
 * `getEntity(id)` (returns the live entity, or a falsy value for an unknown
 * id), `stage(entity)` (optional custom staged clone — default shallow
 * spread), `populate(staged, entity)`, `buildOps(id, staged, entity)`
 * (returns an array of Promises, each resolving after its own local-state
 * patch), `render()`; optional `afterSave(id, staged, entity)` (awaited
 * after the ops settle and before the final `render()`, with the same
 * pre-save `staged`/`entity` `buildOps` saw — e.g. a derived-value refetch
 * or updating the dialog's own title text), `savedToast` (default `'Saved'`),
 * `failedToast` (default `'Failed to save'`). Opening/closing the dialog
 * element itself (`openDialog`/`closeDialog`, backdrop click, the close
 * button) stays module-owned, since every caller also clears its own
 * `state.selectedXId` on close — not worth threading through this config
 * for what's a two-line wrapper either way.
 *
 * Returns `{open, close, save, markDirty, clearDirty}` plus live `id` and
 * `staged` getters so a module's own field listeners (e.g. a toggle click)
 * can read the open entity's id and mutate the staged clone in place.
 */

'use strict';

import { toast } from './state.js';
import { reportActionFailure } from './api.js';
import { closeDialog, openDialog } from './dialog.js';

export function detailModal(config) {
  let entityId = null;
  let staged = null;

  function markDirty() {
    if (config.saveButton) config.saveButton.disabled = false;
  }

  function clearDirty() {
    if (config.saveButton) config.saveButton.disabled = true;
  }

  function open(id) {
    const entity = config.getEntity(id);
    if (!entity) return null;
    entityId = id;
    staged = config.stage ? config.stage(entity) : Object.assign({}, entity);
    config.populate(staged, entity);
    clearDirty();
    openDialog(config.dialog);
    if (config.focusEl) config.focusEl.focus();
    return staged;
  }

  function close() {
    entityId = null;
    staged = null;
    clearDirty();
    closeDialog(config.dialog);
  }

  async function save() {
    const id = entityId;
    // Not a plain `!id` check: a RISCO zone id can legitimately be 0.
    if (id === null || id === undefined || !staged) return false;
    const entity = config.getEntity(id);
    if (!entity) return false;
    if (config.saveButton) config.saveButton.disabled = true;
    const ops = config.buildOps(id, staged, entity);
    try {
      await Promise.all(ops);
      if (config.afterSave) await config.afterSave(id, staged, entity);
      config.render();
      clearDirty();
      toast(config.savedToast || 'Saved', 'success');
      return true;
    } catch (exc) {
      reportActionFailure(exc, config.failedToast || 'Failed to save');
      if (config.saveButton) config.saveButton.disabled = false;
      return false;
    }
  }

  return {
    open: open,
    close: close,
    save: save,
    markDirty: markDirty,
    clearDirty: clearDirty,
    get id() { return entityId; },
    get staged() { return staged; },
  };
}
