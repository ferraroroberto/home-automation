/* UPS power-event notification toggles (Devices tab).
 *
 * A folded-by-default card mirroring the alarm Notifications card. Switches
 * map 1:1 to the backend PowerNotifyPrefs; each persists on click via
 * PUT /api/ups/notify-prefs. A hint shows when Telegram isn't configured.
 * The load/save/render flow is shared with the alarm card via toggle-prefs.js.
 */

'use strict';

import { createTogglePrefs } from './toggle-prefs.js';

const prefs = createTogglePrefs({
  fields: [
    ['notifyPowerLost', 'power_lost'],
    ['notifyPowerRestored', 'power_restored'],
  ],
  url: '/api/ups/notify-prefs',
  noteEl: 'powerNotifyConfiguredNote',
  labels: {
    loadFailed: 'Power notification settings failed',
    saveFailed: 'Notifications save failed',
    saved: 'Notifications saved',
  },
});

export const loadPowerNotifyPrefs = prefs.load;
export const wirePowerNotify = prefs.wire;
