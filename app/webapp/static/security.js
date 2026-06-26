/* RISCO Security tab — boot/core controller.
 *
 * Thin orchestrator (issue #197 maintainability split). Owns the tab poll
 * lifecycle, the top-level renderSecurity() that redraws every card, and the
 * initial loads; the feature logic lives in three sibling modules:
 *   - ./security-alarm.js     alarm state, action pills, detectors + zone modals
 *   - ./security-schedules.js weekly alarm-schedule CRUD
 *   - ./presence.js           presence card, location, automation, push
 *
 * main.js imports five names from here. onSecurityTab lives in this module; the
 * four wire* functions are re-exported from the sub-modules that now own them,
 * so main.js's single import line keeps working unchanged.
 */

'use strict';

import { state, els, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { renderState, renderActions, renderEvents, renderZones } from './security-alarm.js';
import { renderSchedules, loadSecuritySchedules } from './security-schedules.js';
import { renderPresence, loadPresence, loadLocation, loadPresenceAutomation } from './presence.js';

// Re-export the wiring entry points from their new homes so main.js's single
// import from './security.js' continues to resolve all five names.
export { wireZoneDetail, wireSecurityHiddenToggle } from './security-alarm.js';
export { wireSecuritySchedules } from './security-schedules.js';
export { wirePresenceControls } from './presence.js';

const POLL_MS = 10_000;

let securityTimer = null;

export function renderSecurity() {
  renderState();
  renderActions();
  renderSchedules();
  renderEvents();
  renderZones();
  renderPresence();
}

async function loadSecurityState() {
  state.security = await jsonApi('/api/security');
  renderSecurity();
  if (state.tab === 'security') loadPresence();
}

export async function loadSecurity() {
  try {
    const results = await Promise.all([
      jsonApi('/api/security'),
      jsonApi('/api/security/events?count=50'),
      state.tab === 'security' ? jsonApi('/api/security/schedules') : Promise.resolve(null),
    ]);
    state.security = results[0];
    state.securityEvents = (results[1] && results[1].events) || [];
    if (results[2]) state.securitySchedules = results[2].entries || [];
    reportFetchOk('security');
    renderSecurity();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    // The inline note keeps the reason in place; the toast surfaces it once.
    reportFetchFailure('security', exc, 'security');
    state.security = null;
    state.securityEvents = [];
    renderSecurity();
    els.securityEventsNote.hidden = false;
    els.securityEventsNote.textContent = exc.message || 'Failed to load security.';
  }
}

function schedule(ms) {
  if (securityTimer) clearInterval(securityTimer);
  securityTimer = ms > 0 ? setInterval(loadSecurityState, ms) : null;
}

export function onSecurityTab(tab) {
  // The alarm tile is actionable on Home too, so keep it loaded + polling there
  // as well as on the Security tab (issue #72).
  if (tab === 'security' || tab === 'home') {
    loadSecurity();
    if (tab === 'security') {
      loadPresence();
      loadLocation();
      loadPresenceAutomation();
      loadSecuritySchedules();
    }
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
