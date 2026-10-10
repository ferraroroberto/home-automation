/* Settings › Notifications › Alarm — automatic-alarm Telegram toggles.
 *
 * A Settings sheet since #886. Seven switches map 1:1 to the backend
 * AlarmNotifyPrefs; each persists on click via PUT /api/security/notify-prefs.
 * Manual arm/disarm is never notified, so it has no toggle — the sheet's note
 * says so. A hint shows when Telegram isn't set up. The load/save/render flow
 * is shared with the UPS sheet via toggle-prefs.js.
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
  valueEl: 'notifyAlarmValue',
  labels: {
    loadFailed: 'Notification settings failed',
    saveFailed: 'Notifications save failed',
    saved: 'Notifications saved',
  },
});

export const loadNotifyPrefs = prefs.load;
export const wireSecurityNotify = prefs.wire;
