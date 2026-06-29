/* UPS power-event notification toggles (Plugs tab).
 *
 * A folded-by-default card mirroring the alarm Notifications card. Two checkboxes
 * map 1:1 to the backend PowerNotifyPrefs; each persists on change via
 * PUT /api/ups/notify-prefs. A hint shows when Telegram isn't configured.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi } from './api.js';

const FIELDS = [
  ['notifyPowerLost', 'power_lost'],
  ['notifyPowerRestored', 'power_restored'],
];

function renderConfiguredNote(configured) {
  if (!els.powerNotifyConfiguredNote) return;
  if (configured) {
    els.powerNotifyConfiguredNote.hidden = true;
    els.powerNotifyConfiguredNote.textContent = '';
  } else {
    els.powerNotifyConfiguredNote.hidden = false;
    els.powerNotifyConfiguredNote.textContent =
      'Telegram is not configured — set bot_token and chat_id in config/notify_config.json to receive alerts.';
  }
}

function applyPrefs(payload) {
  const prefs = (payload && payload.prefs) || {};
  FIELDS.forEach(function ([elKey, prefKey]) {
    if (els[elKey]) els[elKey].checked = prefs[prefKey] === true;
  });
  renderConfiguredNote(!!(payload && payload.telegram_configured));
}

export async function loadPowerNotifyPrefs() {
  if (!els.notifyPowerLost) return;
  try {
    applyPrefs(await jsonApi('/api/ups/notify-prefs'));
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Power notification settings failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function savePowerNotifyPrefs() {
  const payload = {};
  FIELDS.forEach(function ([elKey, prefKey]) {
    if (els[elKey]) payload[prefKey] = els[elKey].checked;
  });
  try {
    applyPrefs(
      await jsonApi('/api/ups/notify-prefs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
    );
    toast('Notifications saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Notifications save failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wirePowerNotify() {
  FIELDS.forEach(function ([elKey]) {
    if (els[elKey]) els[elKey].addEventListener('change', savePowerNotifyPrefs);
  });
}
