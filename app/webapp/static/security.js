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
// The full alarm-control row, in display order. Always rendered; the live state
// machine decides which are tappable and which is the current (selected) one.
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

function actionAvailable(action) {
  if (!supported(action)) return false;
  const mode = currentMode();
  // Disarmed: only the arm options are actionable (Disarm is the current state).
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

// Toast wording for an action, evaluated before the POST.
function actionToast(action) {
  if (action === 'disarm') return 'Disarming';
  return ACTION_TOASTS[action] || 'Working…';
}

async function postAction(action) {
  if (!actionAvailable(action)) return;
  toast(actionToast(action));  // optimistic — fires the instant you tap (neutral toast)
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

// Alarm controls render into every registered container — the Security tab and
// the Home tab both show the same actionable pills (issue #72). The full row
// (Disarm · Partial · Perimeter · Full) always renders: each reachable action is
// a tappable translucent colour pill, the rest fade out. The current state is
// not specially highlighted on the pills — the "Alarm state: …" line carries it.
function renderActionsInto(el) {
  if (!el) return;
  el.innerHTML = '';
  ACTIONS.forEach(function (action) {
    const available = actionAvailable(action);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'security-action security-action-' + action;
    btn.textContent = ACTION_LABELS[action];
    btn.disabled = !available;
    if (btn.disabled) {
      btn.title = currentMode() === 'unknown' ? 'State unavailable' : 'Unavailable in current state';
    }
    if (available) {
      btn.addEventListener('click', function () { postAction(action); });
    }
    el.appendChild(btn);
  });
}

function renderActions() {
  renderActionsInto(els.securityActions);
  renderActionsInto(els.homeSecurityActions);
}

function renderStateInto(el) {
  if (!el) return;
  const security = state.security;
  const mode = security ? currentMode() : 'unknown';
  const label = security ? displayLabel() : '—';
  el.className = 'security-state ' + statusClass(mode);
  el.innerHTML = '';
  const prefix = document.createElement('span');
  prefix.textContent = 'Alarm state: ';
  el.appendChild(prefix);
  const word = document.createElement('span');
  word.className = 'security-state-word';
  word.textContent = label;
  el.appendChild(word);
}

function renderState() {
  renderStateInto(els.securityState);
  renderStateInto(els.homeSecurityState);
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
  // The alarm tile is actionable on Home too, so keep it loaded + polling there
  // as well as on the Security tab (issue #72).
  if (tab === 'security' || tab === 'home') {
    loadSecurity();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
