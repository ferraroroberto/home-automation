/**
 * The opt-in nightly speed test switch on the internet tile (#840).
 *
 * One boolean, off by default, persisted server-side
 * (`/api/network/speedtest-prefs`); the schedule itself runs in the webapp
 * process, never here. The switch is optimistic and reverts on a failed save,
 * so what it shows is always what the server holds.
 */

import { els, toast } from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { setToggleState, wireToggle } from './toggle.js';

const PREFS_URL = '/api/network/speedtest-prefs';

function applyPrefs(body) {
  setToggleState(els.netNightlyToggle, !!(body && body.prefs && body.prefs.nightly_enabled));
}

async function saveNightly(next) {
  try {
    applyPrefs(await jsonApi(PREFS_URL, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nightly_enabled: next }),
    }));
    toast(next ? 'Nightly speed test on' : 'Nightly speed test off', 'success');
  } catch (exc) {
    setToggleState(els.netNightlyToggle, !next);
    reportActionFailure(exc, 'Nightly speed test save failed');
  }
}

/** Read the saved opt-in; a failed read leaves the switch where it was. */
export async function loadNightlyPref() {
  if (!els.netNightlyToggle) return;
  try {
    applyPrefs(await jsonApi(PREFS_URL));
  } catch (exc) {
    if (!isAuthRequired(exc)) console.warn('nightly speed-test pref unavailable', exc);
  }
}

export function wireNightlyToggle() {
  wireToggle(els.netNightlyToggle, saveNightly);
}
