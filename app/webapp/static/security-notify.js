/* Notifications card — automatic-alarm Telegram toggles.
 *
 * A folded-by-default card under Presence in the Security tab. Five checkboxes
 * map 1:1 to the backend AlarmNotifyPrefs; each persists on change via
 * PUT /api/security/notify-prefs. Manual arm/disarm is never notified, so it has
 * no toggle — the card's note says so. A hint shows when Telegram isn't set up.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi } from './api.js';

const FIELDS = [
  ['notifyScheduleArm', 'schedule_arm'],
  ['notifyScheduleDisarm', 'schedule_disarm'],
  ['notifyPresenceArm', 'presence_arm'],
  ['notifyPresenceDisarm', 'presence_disarm'],
  ['notifyError', 'error'],
];

function renderConfiguredNote(configured) {
  if (!els.notifyConfiguredNote) return;
  if (configured) {
    els.notifyConfiguredNote.hidden = true;
    els.notifyConfiguredNote.textContent = '';
  } else {
    els.notifyConfiguredNote.hidden = false;
    els.notifyConfiguredNote.textContent =
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

export async function loadNotifyPrefs() {
  if (!els.notifyError) return;
  try {
    applyPrefs(await jsonApi('/api/security/notify-prefs'));
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Notification settings failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function saveNotifyPrefs() {
  const payload = {};
  FIELDS.forEach(function ([elKey, prefKey]) {
    if (els[elKey]) payload[prefKey] = els[elKey].checked;
  });
  try {
    applyPrefs(
      await jsonApi('/api/security/notify-prefs', {
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

export function wireSecurityNotify() {
  FIELDS.forEach(function ([elKey]) {
    if (els[elKey]) els[elKey].addEventListener('change', saveNotifyPrefs);
  });
}
