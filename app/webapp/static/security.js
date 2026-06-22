/* RISCO Security tab controller.
 *
 * Owns the alarm state, event log, and detector bypass toggles. Reads are async
 * through GET /api/security and GET /api/security/events; writes are one-tap
 * POST calls that re-render from the returned live state.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
  SECURITY_SHOW_HIDDEN_KEY,
} from './state.js';
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
  prefix.textContent = 'Alarm state:';
  el.appendChild(prefix);
  const word = document.createElement('span');
  word.className = 'security-state-word';
  word.textContent = label;
  el.appendChild(word);
  // System-wide low-battery alert. The cloud exposes no per-detector battery, so
  // this aggregate flag is the "something needs attention → drill in" signal on
  // both the Home and Security tiles (issue #84). Clears when the flag is false.
  if (security && security.battery_low) {
    const badge = document.createElement('span');
    badge.className = 'security-battery-badge';
    badge.textContent = '⚠ Low battery';
    badge.title = 'A detector reports a low battery — check the detectors list';
    el.appendChild(badge);
  }
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

// Build the flags row, rendering each flag as its own span so "Trouble" can
// carry the amber attention colour (matching the low-battery badge) while
// Active/Bypass/Triggered keep their state colour (issue #104).
function renderZoneFlags(zone) {
  const flags = document.createElement('span');
  flags.className = 'security-zone-flags';
  const parts = [];
  if (zone.triggered) parts.push({ text: 'Triggered', cls: '' });
  parts.push({ text: zone.bypassed ? 'Bypass' : 'Active', cls: '' });
  if (zone.trouble) parts.push({ text: 'Trouble', cls: 'is-trouble' });
  parts.forEach(function (part, i) {
    if (i > 0) flags.appendChild(document.createTextNode(' · '));
    const span = document.createElement('span');
    span.className = 'security-zone-flag' + (part.cls ? ' ' + part.cls : '');
    span.textContent = part.text;
    flags.appendChild(span);
  });
  return flags;
}

function renderZones() {
  els.securityZones.innerHTML = '';
  const zones = (state.security && state.security.zones) || [];
  if (!zones.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'No detectors.';
    if (els.securityHiddenCount) els.securityHiddenCount.hidden = true;
    if (els.securityHiddenToggle) els.securityHiddenToggle.hidden = true;
    return;
  }
  els.securityZonesNote.hidden = true;

  // A–Z by display label (mirrors the plugs list); locale-aware so accented
  // Spanish detector names sort naturally.
  const sorted = zones.slice().sort(function (a, b) {
    return zoneLabel(a).localeCompare(zoneLabel(b), undefined, { sensitivity: 'base' });
  });

  // Hidden detectors drop out unless "show hidden" is on, where they render
  // dimmed so they can be un-hidden from the modal (issue #104).
  const hiddenCount = sorted.filter(function (z) { return z.hidden; }).length;
  const visible = state.securityShowHidden
    ? sorted
    : sorted.filter(function (z) { return !z.hidden; });

  if (els.securityHiddenCount) {
    if (hiddenCount > 0) {
      els.securityHiddenCount.textContent = hiddenCount + ' hidden';
      els.securityHiddenCount.hidden = false;
    } else {
      els.securityHiddenCount.hidden = true;
    }
  }
  if (els.securityHiddenToggle) {
    els.securityHiddenToggle.hidden = hiddenCount === 0;
    els.securityHiddenToggle.textContent = state.securityShowHidden ? 'Hide' : 'Show hidden';
    els.securityHiddenToggle.classList.toggle('active', state.securityShowHidden);
  }

  if (!visible.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'All detectors hidden.';
  }

  visible.forEach(function (zone) {
    const row = document.createElement('div');
    row.className = 'security-zone';
    if (zone.triggered) row.classList.add('is-triggered');
    if (zone.bypassed) row.classList.add('is-bypassed');
    else row.classList.add('is-active');
    if (zone.hidden) row.classList.add('is-hidden');

    const main = document.createElement('div');
    main.className = 'security-zone-main';

    // The name opens the detector detail/rename modal (mirrors the AC/plug card
    // header). A button keeps it keyboard-reachable without nesting interactive
    // controls inside the bypass toggle.
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'security-zone-name';
    name.textContent = zoneLabel(zone);
    name.title = 'Detector details · rename';
    name.addEventListener('click', function () { openZoneDetail(zone.id); });
    main.appendChild(name);

    main.appendChild(renderZoneFlags(zone));
    row.appendChild(main);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    const active = !zone.bypassed;
    toggle.className = 'toggle security-bypass' + (active ? ' on' : ' off');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', active ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Detector active ' + zoneLabel(zone));
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

// --------------------------------------------------- detector detail + rename
function zoneLabel(zone) {
  return zone.display_name || zone.name || ('Zone ' + zone.id);
}

function zoneById(zoneId) {
  const zones = (state.security && state.security.zones) || [];
  return zones.find(function (z) { return z.id === zoneId; }) || null;
}

function openZoneDetail(zoneId) {
  const zone = zoneById(zoneId);
  if (!zone) return;
  state.selectedZoneId = zoneId;
  els.zoneDetailName.textContent = zoneLabel(zone);
  els.zoneDetailType.textContent = zone.type === null || zone.type === undefined
    ? '—' : ('Type ' + zone.type);
  els.zoneDetailStatus.textContent = zone.triggered
    ? 'Triggered' : (zone.bypassed ? 'Bypassed' : 'Active');
  els.zoneDetailTrouble.textContent = zone.trouble ? '⚠ Yes' : 'No';
  els.zoneDisplayName.value = zone.display_name || '';
  els.zoneDisplayName.placeholder = zone.name || 'Custom label…';
  // Original RISCO name, so the custom label maps back to the physical detector.
  if (els.zoneOriginalName) {
    els.zoneOriginalName.textContent = 'System name: ' + (zone.name || ('Zone ' + zone.id));
  }
  renderZoneHiddenToggle(zone);
  if (typeof els.zoneDialog.showModal === 'function') els.zoneDialog.showModal();
  else els.zoneDialog.setAttribute('open', '');
  els.zoneDisplayName.focus();
}

function renderZoneHiddenToggle(zone) {
  const btn = els.zoneHiddenToggle;
  if (!btn) return;
  const hidden = !!zone.hidden;
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
    (hidden ? 'ON' : 'OFF') + '</span>';
}

async function toggleZoneHidden() {
  const id = state.selectedZoneId;
  if (id === null || id === undefined) return;
  const zone = zoneById(id);
  if (!zone) return;
  const next = !zone.hidden;
  try {
    await jsonApi('/api/security/zones/' + encodeURIComponent(id) + '/hidden', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden: next }),
    });
    if (state.security && Array.isArray(state.security.zones)) {
      state.security.zones = state.security.zones.map(function (z) {
        return z.id === id ? Object.assign({}, z, { hidden: next }) : z;
      });
    }
    renderZoneHiddenToggle(zoneById(id) || zone);
    renderZones();
    toast(next ? 'Detector hidden' : 'Detector shown', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to update detector: ' + (exc.message || exc), 'error');
    }
  }
}

function closeZoneDetail() {
  state.selectedZoneId = null;
  if (typeof els.zoneDialog.close === 'function') els.zoneDialog.close();
  else els.zoneDialog.removeAttribute('open');
}

async function saveZoneName() {
  if (state.selectedZoneId === null || state.selectedZoneId === undefined) return;
  const id = state.selectedZoneId;
  const newName = els.zoneDisplayName.value.trim();
  try {
    await jsonApi('/api/security/zones/' + encodeURIComponent(id) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    if (state.security && Array.isArray(state.security.zones)) {
      state.security.zones = state.security.zones.map(function (z) {
        return z.id === id ? Object.assign({}, z, { display_name: newName || null }) : z;
      });
    }
    const zone = zoneById(id);
    if (zone) els.zoneDetailName.textContent = zoneLabel(zone);
    renderZones();
    toast('Name saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

// Wire the detector detail/rename modal once at boot (mirrors wirePlugDetail).
export function wireZoneDetail() {
  els.zoneDetailClose.addEventListener('click', closeZoneDetail);
  els.zoneDialog.addEventListener('click', function (ev) {
    if (ev.target === els.zoneDialog) closeZoneDetail();  // backdrop click
  });
  els.zoneDisplayName.addEventListener('blur', saveZoneName);
  els.zoneDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.zoneDisplayName.blur(); }
  });
  if (els.zoneHiddenToggle) {
    els.zoneHiddenToggle.addEventListener('click', toggleZoneHidden);
  }
}

// Wire the "show hidden" detectors toggle in the Detectors header (issue #104).
// The button lives in the <summary>, so swallow the click so it flips the filter
// instead of collapsing the card.
export function wireSecurityHiddenToggle() {
  try {
    if (localStorage.getItem(SECURITY_SHOW_HIDDEN_KEY) === 'true') {
      state.securityShowHidden = true;
    }
  } catch (_) { /* private mode */ }

  if (!els.securityHiddenToggle) return;
  els.securityHiddenToggle.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    state.securityShowHidden = !state.securityShowHidden;
    try {
      localStorage.setItem(SECURITY_SHOW_HIDDEN_KEY, String(state.securityShowHidden));
    } catch (_) { /* private mode */ }
    renderZones();
  });
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
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
