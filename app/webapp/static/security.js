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
import { emptyStateEl } from './empty-state.js';
import { renderState, renderActions, renderEvents, renderZones } from './security-alarm.js';
import { renderSchedules, loadSecuritySchedules } from './security-schedules.js';
import { renderScenePairings, loadScenePairings } from './security-scene.js';
import { renderSecurityOverrides, loadSecurityOverrides } from './security-override.js';
import { renderPresence, loadPresence, loadLocation, loadPresenceAutomation } from './presence.js';
import { loadNotifyPrefs } from './security-notify.js';
import { createPoller } from './poll.js';

// Re-export the wiring entry points from their new homes so main.js's single
// import from './security.js' continues to resolve all the names.
export { wireZoneDetail, wireSecurityHiddenToggle } from './security-alarm.js';
export { wireSecuritySchedules } from './security-schedules.js';
export { wireScenePairings } from './security-scene.js';
export { wireSecurityOverrides } from './security-override.js';
export { wirePresenceControls } from './presence.js';
export { wireSecurityNotify } from './security-notify.js';

const POLL_MS = 10_000;

let securityViewState = 'idle';
let securityUpdatedAt = null;

function setSecurityViewState(next, opts) {
  securityViewState = next;
  if (opts && opts.updatedAt) securityUpdatedAt = opts.updatedAt;
}

function lastUpdatedLabel() {
  const updated = securityUpdatedAt instanceof Date
    ? securityUpdatedAt
    : new Date(securityUpdatedAt || '');
  if (Number.isNaN(updated.getTime())) return 'Last updated earlier';
  return 'Last updated ' + updated.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function renderSecurityFeedback() {
  if (!els.paneSecurity || !els.securityFeedback) return;
  els.paneSecurity.dataset.state = securityViewState;
  els.securityFeedback.innerHTML = '';
  els.securityFeedback.hidden = false;

  if (securityViewState === 'loading') {
    els.securityFeedback.appendChild(
      emptyStateEl('shield-check', 'Reading security status…')
    );
  } else if (securityViewState === 'error') {
    els.securityFeedback.appendChild(emptyStateEl('shield-check', 'Security unavailable', {
      actionLabel: 'Retry',
      onAction: function () { loadSecurity(); },
    }));
  } else if (securityViewState === 'stale') {
    const note = document.createElement('p');
    note.className = 'muted small security-stale-note';
    note.textContent = lastUpdatedLabel() + ' · live data unavailable';
    els.securityFeedback.appendChild(note);
  } else {
    els.securityFeedback.hidden = true;
  }

  if (!els.homeSecurityFeedback) return;
  if (securityViewState === 'loading') {
    els.homeSecurityFeedback.textContent = 'Reading security status…';
    els.homeSecurityFeedback.hidden = false;
  } else if (securityViewState === 'error') {
    els.homeSecurityFeedback.textContent = 'Security unavailable';
    els.homeSecurityFeedback.hidden = false;
  } else if (securityViewState === 'stale') {
    els.homeSecurityFeedback.textContent = lastUpdatedLabel() + ' · live data unavailable';
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
  setSecurityViewState(state.security ? 'stale' : 'error');
  reportFetchFailure(
    'security',
    { message: 'live data unavailable' },
    'security'
  );
  renderSecurityFeedback();
  disableSecurityActions();
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
  renderSecurityFeedback();
  if (securityViewState === 'stale') disableSecurityActions();
}

async function loadSecurityState() {
  try {
    state.security = await jsonApi('/api/security');
    reportFetchOk('security');
    setSecurityViewState('ready', { updatedAt: new Date() });
    renderSecurity();
    if (state.tab === 'security') loadPresence();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    markSecurityFailure();
  }
}

export async function loadSecurity() {
  if (!state.security) {
    setSecurityViewState('loading');
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
    setSecurityViewState('ready', { updatedAt: new Date() });
    renderSecurity();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    markSecurityFailure();
  }
}

const schedule = createPoller(loadSecurityState);

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
      loadScenePairings();
      loadSecurityOverrides();
      loadNotifyPrefs();
    }
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
