/* Shared Telegram-notification toggle-prefs card (issue #746).
 *
 * The alarm Notifications card (security-notify.js) and the UPS power-event
 * card (ups-notify.js) are the same control flow over different field lists,
 * element ids, URLs and labels. `createTogglePrefs` owns that flow once —
 * load, save-on-click, render the switches, and the "Telegram is not
 * configured" hint — so a fix (error handling, the configured-note logic) is
 * made in one place. Mirrors the server side's `make_bool_prefs_router`.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi, reportActionFailure } from './api.js';
import { setToggleState, isToggleOn, wireToggle } from './toggle.js';

const NOT_CONFIGURED_NOTE =
  'Telegram is not configured — set bot_token and chat_id in config/notify_config.json to receive alerts.';

/**
 * @param {object} cfg
 * @param {Array<[string, string]>} cfg.fields  [els key, pref key] pairs.
 * @param {string} cfg.url                      GET/PUT prefs endpoint.
 * @param {string} cfg.noteEl                   els key of the "not configured" note.
 * @param {string} cfg.valueEl                  els key of the Settings row value (#886).
 * @param {{loadFailed: string, saveFailed: string, saved: string}} cfg.labels
 * @returns {{load: function(): Promise<void>, wire: function(): void}}
 */
export function createTogglePrefs({ fields, url, noteEl, valueEl, labels }) {
  function renderConfiguredNote(configured) {
    const note = els[noteEl];
    if (!note) return;
    note.hidden = !!configured;
    note.textContent = configured ? '' : NOT_CONFIGURED_NOTE;
  }

  // The Settings row reads without opening its sheet: how many alerts are on,
  // or that none can arrive because Telegram is not set up.
  function renderValue(prefs, configured) {
    const value = els[valueEl];
    if (!value) return;
    const on = fields.filter(function ([, prefKey]) { return prefs[prefKey] === true; }).length;
    value.textContent = configured ? on + ' of ' + fields.length + ' on' : 'Telegram not set up';
  }

  function applyPrefs(payload) {
    const prefs = (payload && payload.prefs) || {};
    const configured = !!(payload && payload.telegram_configured);
    fields.forEach(function ([elKey, prefKey]) {
      if (els[elKey]) setToggleState(els[elKey], prefs[prefKey] === true);
    });
    renderConfiguredNote(configured);
    renderValue(prefs, configured);
  }

  async function load() {
    if (!els[fields[0][0]]) return;
    try {
      applyPrefs(await jsonApi(url));
    } catch (exc) {
      reportActionFailure(exc, labels.loadFailed);
    }
  }

  async function save() {
    const payload = {};
    fields.forEach(function ([elKey, prefKey]) {
      if (els[elKey]) payload[prefKey] = isToggleOn(els[elKey]);
    });
    try {
      applyPrefs(
        await jsonApi(url, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
      );
      toast(labels.saved, 'success');
    } catch (exc) {
      reportActionFailure(exc, labels.saveFailed);
    }
  }

  function wire() {
    fields.forEach(function ([elKey]) {
      wireToggle(els[elKey], save);
    });
  }

  return { load, wire };
}
