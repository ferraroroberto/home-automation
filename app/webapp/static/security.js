/* RISCO Security tab — boot/core controller.
 *
 * Thin orchestrator (issue #197 maintainability split). Owns the tab poll
 * lifecycle, the top-level renderSecurity() that redraws every group, the
 * group link rows' sheets (#882), and the initial loads; the feature logic
 * lives in three sibling modules:
 *   - ./security-alarm.js     alarm state, action pills, detectors + zone modals
 *   - ./security-schedules.js weekly alarm-schedule CRUD
 *   - ./presence.js           presence card, location, automation, push
 *
 * main.js imports every Security name from here. onSecurityTab and
 * wireSecuritySheets live in this module; the other wire* functions are
 * re-exported from the sub-modules that own them, so main.js keeps one import
 * line.
 */

'use strict';

import { state, els, reportFetchOk } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { createViewState, markTabFailure, renderFeedback } from './view-state.js';
import { renderState, renderActions, renderEvents, renderZones } from './security-alarm.js';
import { renderSchedules, loadSecuritySchedules } from './security-schedules.js';
import { renderScenePairings, loadScenePairings } from './security-scene.js';
import { renderSecurityOverrides, loadSecurityOverrides } from './security-override.js';
import { renderPresence, loadPresence, loadLocation, loadPresenceAutomation } from './presence.js';
import { renderPresencePlaces, loadPresencePlaces } from './presence-places.js';
import { loadNotifyPrefs } from './security-notify.js';
import { createPoller } from './poll.js';
import { sheet } from './sheet.js';

// Re-export the wiring entry points from their new homes so main.js's single
// import from './security.js' continues to resolve all the names.
export { wireZoneDetail, wireSecurityHiddenToggle } from './security-alarm.js';
export { wireSecuritySchedules } from './security-schedules.js';
export { wireScenePairings } from './security-scene.js';
export { wireSecurityOverrides } from './security-override.js';
export { wirePresenceControls } from './presence.js';
export { wirePresencePlaces } from './presence-places.js';
export { wireSecurityNotify } from './security-notify.js';

const POLL_MS = 10_000;

// The Security tab's link rows that open a sheet (#882): Automations'
// Schedules / Scene capture / Override (dense collections whose switches save
// as they change, so instant, with Done) and People's Accounts. The sheets'
// lists keep their own renderers and ids; only where they sit moved.
const SECURITY_SHEETS = [
  ['securitySchedulesOpen', 'securitySchedulesSheet'],
  ['scenePairingsOpen', 'scenePairingsSheet'],
  ['securityOverridesOpen', 'securityOverridesSheet'],
  ['presenceAccountsOpen', 'presenceAccountsSheet'],
];

export function wireSecuritySheets() {
  SECURITY_SHEETS.forEach(function (pair) {
    const opener = document.getElementById(pair[0]);
    const dialog = document.getElementById(pair[1]);
    if (!opener || !dialog) return;
    const shell = sheet(dialog, {
      model: 'instant',
      closeButton: document.getElementById(pair[1] + 'Close'),
      doneButton: document.getElementById(pair[1] + 'Done'),
    });
    opener.addEventListener('click', function () { shell.open(opener); });
  });
}

const securityView = createViewState();

function renderSecurityFeedback() {
  if (!els.paneSecurity) return;
  renderFeedback(securityView, els.securityFeedback, {
    paneEl: els.paneSecurity,
    icon: 'shield-check',
    loadingLabel: 'Reading security status…',
    errorLabel: 'Security unavailable',
    staleClass: 'security-stale-note',
    onRetry: function () { loadSecurity(); },
  });

  if (!els.homeSecurityFeedback) return;
  if (securityView.state === 'loading') {
    els.homeSecurityFeedback.textContent = 'Reading security status…';
    els.homeSecurityFeedback.hidden = false;
  } else if (securityView.state === 'error') {
    els.homeSecurityFeedback.textContent = 'Security unavailable';
    els.homeSecurityFeedback.hidden = false;
  } else if (securityView.state === 'stale') {
    els.homeSecurityFeedback.textContent = securityView.lastUpdatedLabel() + ' · live data unavailable';
    els.homeSecurityFeedback.hidden = false;
  } else {
    els.homeSecurityFeedback.hidden = true;
  }
}

function disableSecurityActions() {
  [els.securityActions, els.homeSecurityActions].forEach(function (container) {
    if (!container) return;
    container.querySelectorAll('.security-action').forEach(function (button) {
      button.disabled = true;
      button.title = 'Live security state unavailable';
    });
  });
}

function markSecurityFailure() {
  markTabFailure(securityView, {
    hasData: !!state.security,
    scope: 'security',
    label: 'security',
    render: function () {
      renderSecurityFeedback();
      disableSecurityActions();
    },
  });
}

export function renderSecurity() {
  renderState();
  renderActions();
  renderSchedules();
  renderScenePairings();
  renderSecurityOverrides();
  renderEvents();
  renderZones();
  renderPresence();
  renderPresencePlaces();
  renderSecurityFeedback();
  if (securityView.state === 'stale') disableSecurityActions();
}

async function loadSecurityState() {
  try {
    state.security = await jsonApi('/api/security');
    reportFetchOk('security');
    securityView.set('ready', { updatedAt: new Date() });
    renderSecurity();
    // Presence also polls on Home (the locator card, issue #438) — same
    // precedent as the alarm tile being actionable on Home too (issue #72).
    if (state.tab === 'security' || state.tab === 'home') loadPresence();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markSecurityFailure();
  }
}

export async function loadSecurity() {
  if (!state.security) {
    securityView.set('loading');
    renderSecurityFeedback();
  }
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
    securityView.set('ready', { updatedAt: new Date() });
    renderSecurity();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markSecurityFailure();
  }
}

const schedule = createPoller(loadSecurityState);

export function onSecurityTab(tab) {
  // The alarm tile is actionable on Home too, so keep it loaded + polling there
  // as well as on the Security tab (issue #72).
  if (tab === 'security' || tab === 'home') {
    loadSecurity();
    loadPresence();
    if (tab === 'security') {
      // The Kids-home pill on the Presence card reads the automation prefs.
      loadPresenceAutomation();
      loadSecuritySchedules();
      loadScenePairings();
      loadSecurityOverrides();
    }
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
  // Presence settings, Places and the alarm notification toggles live in
  // Settings since #779 — one read on entry, no polling.
  if (tab === 'settings') {
    loadLocation();
    loadPresenceAutomation();
    loadPresencePlaces();
    loadNotifyPrefs();
  }
}
