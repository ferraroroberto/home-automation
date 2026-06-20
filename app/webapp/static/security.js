/* RISCO Security tab controller.
 *
 * Owns the alarm state, event log, and detector bypass toggles. Reads are async
 * through GET /api/security and GET /api/security/events; writes are one-tap
 * POST calls that re-render from the returned live state.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';

const POLL_MS = 10_000;
const ACTIONS = ['disarm', 'partial', 'perimeter', 'arm'];
const ACTION_LABELS = {
  disarm: 'Disarm',
  partial: 'Partial',
  arm: 'Full',
  perimeter: 'Perimeter',
};
// Optimistic toast shown the instant an action is tapped (before the refresh).
const ACTION_TOASTS = {
  partial: 'Arming partial',
  perimeter: 'Arming perimeter',
  arm: 'Arming full',
};
const MODE_LABELS = {
  disarmed: 'Not armed',
  armed: 'Fully armed',
  arming: 'Arming',
  partial: 'Partial',
  perimeter: 'Perimeter',
  triggered: 'Triggered',
  unknown: 'Unknown',
};

let securityTimer = null;

function supported(action) {
  const actions = (state.security && state.security.supported_actions) || [];
  return actions.includes(action);
}

function currentMode() {
  const security = state.security || {};
  return security.mode || 'unknown';
}

function displayLabel() {
  const security = state.security || {};
  const mode = security.mode || 'unknown';
  return MODE_LABELS[mode] || security.label || 'Unknown';
}

function statusClass(mode) {
  if (mode === 'triggered') return 'is-alert';
  if (mode === 'disarmed') return 'is-disarmed';
  if (mode === 'armed' || mode === 'arming') return 'is-armed';
  if (mode === 'partial') return 'is-partial';
  if (mode === 'perimeter') return 'is-perimeter';
  return '';
}

// A trouble or alarm-memory condition turns the first button into "Clear".
function hasTroubleOrMemory() {
  const security = state.security || {};
  return security.trouble === true || security.memory_alarm === true;
}

// Which action represents the state the system is currently in (highlighted as
// the active pill). Null while triggered/unknown — nothing to highlight.
function currentAction() {
  const mode = currentMode();
  if (mode === 'disarmed') return 'disarm';
  if (mode === 'armed' || mode === 'arming') return 'arm';
  if (mode === 'partial') return 'partial';
  if (mode === 'perimeter') return 'perimeter';
  return null;
}

function actionAvailable(action) {
  if (!supported(action)) return false;
  // Clear: disarm is tappable whenever a trouble/alarm-memory must be cleared,
  // even from an otherwise-disarmed system.
  if (action === 'disarm' && hasTroubleOrMemory()) return true;
  const mode = currentMode();
  if (mode === 'disarmed') return action !== 'disarm';
  if (mode === 'armed' || mode === 'arming' || mode === 'partial' || mode === 'perimeter') {
    return action === 'disarm';
  }
  if (mode === 'triggered') return action === 'disarm';
  return false;
}

function fmtTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

// Toast wording for an action, evaluated before the POST (so the disarm vs
// clear distinction still sees the current trouble/memory state).
function actionToast(action) {
  if (action === 'disarm') return hasTroubleOrMemory() ? 'Clearing' : 'Disarming';
  return ACTION_TOASTS[action] || 'Working…';
}

async function postAction(action) {
  if (!actionAvailable(action)) return;
  toast(actionToast(action), 'good');  // optimistic — fires the instant you tap
  try {
    state.security = await jsonApi('/api/security/' + encodeURIComponent(action), {
      method: 'POST',
    });
    renderSecurity();
    await loadSecurityEvents();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Security failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function setBypass(zone, bypass) {
  try {
    state.security = await jsonApi(
      '/api/security/zones/' + encodeURIComponent(zone.id) + '/bypass',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bypass: bypass }),
      },
    );
    renderSecurity();
    await loadSecurityEvents();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Bypass failed: ' + (exc.message || exc), 'error');
    }
  }
}

function renderActions() {
  els.securityActions.innerHTML = '';
  const current = currentAction();
  const clearing = hasTroubleOrMemory();
  ACTIONS.forEach(function (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'security-action security-action-' + action;
    // The first button reads "Clear" only while a trouble/alarm-memory is up.
    btn.textContent = action === 'disarm' && clearing ? 'Clear' : ACTION_LABELS[action];
    btn.disabled = !actionAvailable(action);
    // The state the system is in shows as the highlighted (filled) pill, even
    // when it isn't itself a tappable transition.
    if (action === current) btn.classList.add('is-current');
    if (btn.disabled && action !== current) {
      btn.title = currentMode() === 'unknown' ? 'State unavailable' : 'Unavailable in current state';
    }
    btn.addEventListener('click', function () { postAction(action); });
    els.securityActions.appendChild(btn);
  });
}

function renderState() {
  const security = state.security;
  const mode = security ? currentMode() : 'unknown';
  const label = security ? displayLabel() : '—';
  els.securityState.className = 'security-state ' + statusClass(mode);
  els.securityState.innerHTML = '';
  const prefix = document.createElement('span');
  prefix.textContent = 'Current state: ';
  els.securityState.appendChild(prefix);
  const word = document.createElement('span');
  word.className = 'security-state-word';
  word.textContent = label;
  els.securityState.appendChild(word);
}

function renderEvents() {
  els.securityEvents.innerHTML = '';
  const events = state.securityEvents || [];
  if (!events.length) {
    els.securityEventsNote.hidden = false;
    els.securityEventsNote.textContent = 'No recent events.';
    return;
  }
  els.securityEventsNote.hidden = true;

  const hasActor = events.some(function (event) {
    return event.user_id !== null && event.user_id !== undefined && event.user_id !== '' && event.user_id !== 0;
  });

  events.slice(0, 20).forEach(function (event) {
    const row = document.createElement('div');
    row.className = 'security-event';

    const time = document.createElement('span');
    time.className = 'security-event-time';
    time.textContent = fmtTime(event.time);
    row.appendChild(time);

    const body = document.createElement('span');
    body.className = 'security-event-body';
    body.textContent = event.name || event.type || event.category || event.text || 'Event';
    row.appendChild(body);

    if (hasActor) {
      const actor = document.createElement('span');
      actor.className = 'security-event-actor';
      actor.textContent = event.user_id ? ('U' + event.user_id) : '-';
      row.appendChild(actor);
    }

    els.securityEvents.appendChild(row);
  });
}

function renderZones() {
  els.securityZones.innerHTML = '';
  const zones = (state.security && state.security.zones) || [];
  if (!zones.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'No detectors.';
    return;
  }
  els.securityZonesNote.hidden = true;

  zones.forEach(function (zone) {
    const row = document.createElement('div');
    row.className = 'security-zone';
    if (zone.triggered) row.classList.add('is-triggered');
    if (zone.bypassed) row.classList.add('is-bypassed');
    else row.classList.add('is-active');

    const main = document.createElement('div');
    main.className = 'security-zone-main';

    const name = document.createElement('span');
    name.className = 'security-zone-name';
    name.textContent = zone.name || ('Zone ' + zone.id);
    main.appendChild(name);

    const flags = document.createElement('span');
    flags.className = 'security-zone-flags';
    const flagText = [];
    if (zone.triggered) flagText.push('Triggered');
    flagText.push(zone.bypassed ? 'Bypass' : 'Active');
    flags.textContent = flagText.join(' · ');
    main.appendChild(flags);
    row.appendChild(main);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    const active = !zone.bypassed;
    toggle.className = 'toggle security-bypass' + (active ? ' on' : ' off');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', active ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Detector active ' + (zone.name || ('zone ' + zone.id)));
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (active ? 'ON' : 'OFF') + '</span>';
    toggle.addEventListener('click', function () { setBypass(zone, active); });
    row.appendChild(toggle);

    els.securityZones.appendChild(row);
  });
}

export function renderSecurity() {
  renderState();
  renderActions();
  renderEvents();
  renderZones();
}

async function loadSecurityState() {
  state.security = await jsonApi('/api/security');
  renderSecurity();
}

async function loadSecurityEvents() {
  const body = await jsonApi('/api/security/events?count=50');
  state.securityEvents = (body && body.events) || [];
  renderEvents();
}

export async function loadSecurity() {
  try {
    const results = await Promise.all([
      jsonApi('/api/security'),
      jsonApi('/api/security/events?count=50'),
    ]);
    state.security = results[0];
    state.securityEvents = (results[1] && results[1].events) || [];
    renderSecurity();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
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
  if (tab === 'security') {
    loadSecurity();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
