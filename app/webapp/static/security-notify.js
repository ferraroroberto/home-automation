/* Notifications card — automatic-alarm Telegram toggles.
 *
 * A folded-by-default card under Presence in the Security tab. Seven switches
 * map 1:1 to the backend AlarmNotifyPrefs; each persists on click via
 * PUT /api/security/notify-prefs. Manual arm/disarm is never notified, so it has
 * no toggle — the card's note says so. A hint shows when Telegram isn't set up.
 * The load/save/render flow is shared with the UPS card via toggle-prefs.js.
 */

'use strict';

import { createTogglePrefs } from './toggle-prefs.js';

const prefs = createTogglePrefs({
  fields: [
    ['notifyScheduleArm', 'schedule_arm'],
    ['notifyScheduleDisarm', 'schedule_disarm'],
    ['notifyPresenceArm', 'presence_arm'],
    ['notifyPresenceDisarm', 'presence_disarm'],
    ['notifyError', 'error'],
    ['notifyIntrusion', 'intrusion'],
    ['notifyAcLost', 'ac_lost'],
  ],
  url: '/api/security/notify-prefs',
  noteEl: 'notifyConfiguredNote',
  labels: {
    loadFailed: 'Notification settings failed',
    saveFailed: 'Notifications save failed',
    saved: 'Notifications saved',
  },
});

export const loadNotifyPrefs = prefs.load;
export const wireSecurityNotify = prefs.wire;
