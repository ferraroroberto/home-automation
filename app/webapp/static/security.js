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
  PRESENCE_SHOW_HIDDEN_KEY,
  THIS_DEVICE_PRESENCE_KEY,
  THIS_DEVICE_LOCATION_KEY,
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
    second: '2-digit',
  });
}

function fmtDistance(value) {
  if (value === null || value === undefined) return 'unknown';
  const n = Number(value);
  if (!Number.isFinite(n)) return 'unknown';
  if (n >= 1000) return (n / 1000).toFixed(1) + ' km';
  return Math.round(n) + ' m';
}

function coordsKey(entity) {
  if (entity.latitude === null || entity.latitude === undefined ||
      entity.longitude === null || entity.longitude === undefined) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return '';
  return lat.toFixed(4) + ',' + lon.toFixed(4);
}

function placeLabel(entity) {
  const key = coordsKey(entity);
  if (!key) return '';
  const value = state.presencePlaces[key];
  return typeof value === 'string' ? value : '';
}

function ensurePlaceLabel(entity) {
  const key = coordsKey(entity);
  if (!key || state.presencePlaces[key] !== undefined) return;
  state.presencePlaces[key] = null;
  const url = '/api/location/reverse?lat=' + encodeURIComponent(entity.latitude) +
    '&lon=' + encodeURIComponent(entity.longitude);
  jsonApi(url).then(function (body) {
    state.presencePlaces[key] = body && body.available ? (body.label || '') : '';
    const selected = state.selectedPresenceId ? presenceById(state.selectedPresenceId) : null;
    if (selected && coordsKey(selected) === key && els.presenceDetailPlace) {
      els.presenceDetailPlace.textContent = state.presencePlaces[key] || '—';
    }
    renderPresence();
  }).catch(function () {
    state.presencePlaces[key] = '';
  });
}

function presenceLocationText(entity) {
  const place = placeLabel(entity);
  const dist = fmtDistance(entity.distance_from_home_m);
  if (place && dist !== 'unknown') return place + ' · ' + dist;
  return place || dist;
}

function presenceLabel(entity) {
  if (entity.at_home === true) return 'Home';
  if (entity.at_home === false) return 'Away';
  return 'Unknown';
}

function presenceEntityLabel(entity) {
  return entity.display_name || entity.name || entity.entity_id || 'Unknown';
}

function isThisDevice(entity) {
  return entity && entity.entity_id === '__this_device__';
}

function presenceById(entityId) {
  const entities = (state.presence && state.presence.entities) || [];
  if (state.thisDevicePresence && state.thisDevicePresence.entity_id === entityId) {
    return state.thisDevicePresence;
  }
  return entities.find(function (e) { return e.entity_id === entityId; }) || null;
}

function sourceLabel(entity) {
  if (entity.source === 'webhook') return 'Shortcut';
  if (entity.source === 'icloud') return 'Find My';
  if (entity.source === 'browser') return 'Browser GPS · diagnostic only';
  return entity.source || 'Unknown';
}

function renderPresence() {
  if (!els.presenceSummary || !els.presenceList || !els.presenceNote) return;
  els.presenceList.innerHTML = '';
  const presence = state.presence;
  if (!presence) {
    els.presenceSummary.textContent = '—';
    els.presenceNote.hidden = true;
    return;
  }
  if (presence.available === false) {
    els.presenceSummary.textContent = 'Unavailable';
    els.presenceNote.hidden = false;
    els.presenceNote.textContent = presence.reason === '2fa_required'
      ? 'iCloud needs 2FA; run the CLI once to refresh the trusted session.'
      : (presence.detail || 'Presence is not configured.');
    return;
  }

  const entities = (presence.entities || []).concat(state.thisDevicePresence ? [state.thisDevicePresence] : []);
  const sorted = entities.slice().sort(function (a, b) {
    if (a.entity_id === '__this_device__') return -1;
    if (b.entity_id === '__this_device__') return 1;
    return presenceEntityLabel(a).localeCompare(presenceEntityLabel(b), undefined, { sensitivity: 'base' });
  });
  const hiddenCount = sorted.filter(function (e) { return e.hidden && !isThisDevice(e); }).length;
  const visible = state.presenceShowHidden
    ? sorted
    : sorted.filter(function (e) { return !e.hidden || isThisDevice(e); });
  const counted = visible.filter(function (e) { return !isThisDevice(e); });

  const homeCount = counted.filter(function (e) { return e.at_home === true; }).length;
  const awayCount = counted.filter(function (e) { return e.at_home === false; }).length;
  const unknownCount = counted.filter(function (e) { return e.at_home !== true && e.at_home !== false; }).length;
  els.presenceSummary.textContent =
    homeCount + ' home · ' + awayCount + ' away · ' + unknownCount + ' unknown';
  if (els.presenceHiddenCount) {
    if (hiddenCount > 0) {
      els.presenceHiddenCount.textContent = hiddenCount + ' hidden';
      els.presenceHiddenCount.hidden = false;
    } else {
      els.presenceHiddenCount.hidden = true;
    }
  }
  if (els.presenceHiddenToggle) {
    els.presenceHiddenToggle.hidden = hiddenCount === 0;
    els.presenceHiddenToggle.textContent = state.presenceShowHidden ? 'Hide' : 'Show hidden';
    els.presenceHiddenToggle.classList.toggle('active', state.presenceShowHidden);
  }

  els.presenceNote.hidden = visible.length > 0;
  els.presenceNote.textContent = visible.length ? '' : 'No presence entities.';

  visible
    .forEach(function (entity) {
      const row = document.createElement('div');
      row.className = 'presence-row';
      if (entity.hidden) row.classList.add('is-hidden');
      if (entity.stale) row.classList.add('is-stale');
      if (entity.at_home === true) row.classList.add('is-home');
      else if (entity.at_home === false) row.classList.add('is-away');
      else row.classList.add('is-unknown');

      const main = document.createElement('div');
      main.className = 'presence-main';

      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'presence-name';
      name.textContent = presenceEntityLabel(entity);
      name.title = 'Presence details · rename';
      name.addEventListener('click', function () { openPresenceDetail(entity.entity_id); });
      main.appendChild(name);

      const meta = document.createElement('span');
      meta.className = 'presence-meta';
      meta.textContent = [
        sourceLabel(entity),
        entity.device_class || entity.model || 'Device',
        entity.last_seen ? fmtTime(entity.last_seen) : 'not located',
        entity.stale ? 'stale' : '',
      ].filter(Boolean).join(' · ');
      main.appendChild(meta);
      row.appendChild(main);

      const status = document.createElement('span');
      status.className = 'presence-status';
      const statusLine = document.createElement('span');
      statusLine.className = 'presence-status-line';
      const dist = fmtDistance(entity.distance_from_home_m);
      statusLine.textContent = presenceLabel(entity) + (dist !== 'unknown' ? ' · ' + dist : '');
      status.appendChild(statusLine);
      const place = placeLabel(entity);
      if (place) {
        const addrLine = document.createElement('span');
        addrLine.className = 'presence-status-addr';
        addrLine.textContent = place;
        status.appendChild(addrLine);
      }
      row.appendChild(status);
      els.presenceList.appendChild(row);
      ensurePlaceLabel(entity);
    });

  renderPresenceRefreshNote();
  renderPresenceAutomationNote();
}

function renderPresenceRefreshNote() {
  if (!els.presenceRefreshNote) return;
  const diag = (state.presence && state.presence.diagnostics) || {};
  const interval = Math.max(1, Math.round((Number(diag.refresh_interval_s) || 300) / 60));
  const last = diag.refreshed_at ? fmtTime(diag.refreshed_at) : 'not yet';
  els.presenceRefreshNote.textContent =
    'The open tab reloads the local snapshot every 10 s. Find My refreshes in the background about every ' + interval +
    ' min; Refresh runs it now. This device is browser GPS only: it updates only while this tab/PWA is open and is not used for alarm automation. Alarm automation uses Shortcut webhook people. Last Find My refresh: ' + last + '.';
}

function renderPresenceAutomationNote() {
  if (!els.presenceAutomationNote || !els.presenceAutoEnabled) return;
  const entities = (state.presence && state.presence.entities) || [];
  const hasWebhookPerson = entities.some(function (entity) {
    return entity.source === 'webhook' && !entity.hidden;
  });
  if (els.presenceAutoEnabled.checked && !hasWebhookPerson) {
    els.presenceAutomationNote.textContent = 'Configure iOS Shortcut arrive/leave webhooks before enabling alarm automation. Browser GPS and Find My diagnostics do not drive arm/disarm.';
    els.presenceAutomationNote.hidden = false;
  } else {
    els.presenceAutomationNote.hidden = true;
    els.presenceAutomationNote.textContent = '';
  }
}

async function loadPresence() {
  try {
    state.presence = await jsonApi('/api/presence');
    reportFetchOk('presence');
    refreshThisDeviceLocation();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('presence', exc, 'presence');
    state.presence = { available: false, reason: 'error', detail: exc.message || String(exc) };
  }
  renderPresence();
}

function distanceMeters(lat1, lon1, lat2, lon2) {
  const radius = 6371000;
  const p1 = lat1 * Math.PI / 180;
  const p2 = lat2 * Math.PI / 180;
  const dp = (lat2 - lat1) * Math.PI / 180;
  const dl = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dp / 2) * Math.sin(dp / 2) +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
  return radius * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function buildThisDevicePresence(lat, lon, accuracy, lastSeen) {
  const home = state.location || {};
  let distance = null;
  let atHome = null;
  if (Number.isFinite(Number(home.lat)) && Number.isFinite(Number(home.lon))) {
    distance = distanceMeters(Number(home.lat), Number(home.lon), lat, lon);
    atHome = distance <= ((state.presence && state.presence.home_radius_m) || 200);
  }
  const seen = lastSeen || new Date().toISOString();
  const seenAt = new Date(seen).getTime();
  const staleAfterMs = Math.max(10 * 60 * 1000, Number((state.presence && state.presence.refresh_interval_s) || 300) * 2000);
  return {
    entity_id: '__this_device__',
    name: 'This device',
    display_name: null,
    hidden: false,
    model: null,
    device_class: 'Browser',
    latitude: lat,
    longitude: lon,
    horizontal_accuracy_m: accuracy || null,
    last_seen: seen,
    battery_level_pct: null,
    battery_status: null,
    distance_from_home_m: distance,
    at_home: atHome,
    source: 'browser',
    stale: Number.isFinite(seenAt) ? (Date.now() - seenAt > staleAfterMs) : false,
  };
}

function storeThisDeviceLocation(lat, lon, accuracy, lastSeen) {
  try {
    localStorage.setItem(THIS_DEVICE_PRESENCE_KEY, 'true');
    localStorage.setItem(THIS_DEVICE_LOCATION_KEY, JSON.stringify({
      lat: lat,
      lon: lon,
      accuracy: accuracy || null,
      last_seen: lastSeen,
    }));
  } catch (_) { /* private mode */ }
}

function hydrateThisDeviceLocation() {
  try {
    const raw = localStorage.getItem(THIS_DEVICE_LOCATION_KEY);
    let cached = raw ? JSON.parse(raw) : null;
    if (!cached && localStorage.getItem(THIS_DEVICE_PRESENCE_KEY) === 'true') {
      const home = state.location || {};
      if (Number.isFinite(Number(home.lat)) && Number.isFinite(Number(home.lon))) {
        cached = {
          lat: Number(home.lat),
          lon: Number(home.lon),
          accuracy: null,
          last_seen: new Date(Date.now() - 11 * 60 * 1000).toISOString(),
        };
      }
    }
    if (!cached) return false;
    const lat = Number(cached.lat);
    const lon = Number(cached.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return false;
    state.thisDevicePresence = buildThisDevicePresence(lat, lon, Number(cached.accuracy) || null, cached.last_seen);
    return true;
  } catch (_) {
    return false;
  }
}

function updateThisDeviceFromPosition(pos) {
  const lat = pos.coords.latitude;
  const lon = pos.coords.longitude;
  const lastSeen = new Date().toISOString();
  const accuracy = pos.coords.accuracy || null;
  state.thisDevicePresence = buildThisDevicePresence(lat, lon, accuracy, lastSeen);
  storeThisDeviceLocation(lat, lon, accuracy, lastSeen);
  renderPresence();
}

function refreshThisDeviceLocation() {
  if (!navigator.geolocation) return;
  try {
    if (localStorage.getItem(THIS_DEVICE_PRESENCE_KEY) !== 'true') return;
  } catch (_) {
    return;
  }
  navigator.geolocation.getCurrentPosition(updateThisDeviceFromPosition, function () {
    hydrateThisDeviceLocation();
    renderPresence();
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
}

async function refreshPresenceDiagnostics() {
  try {
    await jsonApi('/api/presence/refresh', { method: 'POST' });
    await loadPresence();
    toast('Presence refreshed', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Presence refresh failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function loadLocation() {
  if (!els.locationLat || !els.locationLon) return;
  try {
    state.location = await jsonApi('/api/location');
    els.locationLabel.value = state.location.label || '';
    els.locationLat.value = state.location.lat == null ? '' : state.location.lat;
    els.locationLon.value = state.location.lon == null ? '' : state.location.lon;
    if (hydrateThisDeviceLocation()) renderPresence();
    refreshThisDeviceLocation();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Location failed: ' + (exc.message || exc), 'error');
    }
  }
}

function locationPayload() {
  const lat = Number(els.locationLat.value);
  const lon = Number(els.locationLon.value);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { lat: lat, lon: lon, label: (els.locationLabel.value || '').trim() };
}

async function saveLocation() {
  const payload = locationPayload();
  if (!payload) return;
  try {
    state.location = await jsonApi('/api/location', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast('Location saved', 'success');
    await refreshPresenceDiagnostics();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Location failed: ' + (exc.message || exc), 'error');
    }
  }
}

function useBrowserLocation() {
  if (!navigator.geolocation) {
    toast('Location unavailable in this browser', 'error');
    return;
  }
  navigator.geolocation.getCurrentPosition(function (pos) {
    els.locationLat.value = pos.coords.latitude.toFixed(6);
    els.locationLon.value = pos.coords.longitude.toFixed(6);
    if (!els.locationLabel.value.trim()) els.locationLabel.value = 'Home';
    updateThisDeviceFromPosition(pos);
    saveLocation();
  }, function (err) {
    toast('Location failed: ' + err.message, 'error');
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
}

async function loadPresenceAutomation() {
  if (!els.presenceAutoEnabled) return;
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation');
    const cfg = state.presenceAutomation || {};
    els.presenceAutoEnabled.checked = cfg.enabled === true;
    els.presenceArmMinutes.value = Math.round((Number(cfg.arm_away_after_s) || 0) / 60);
    els.presenceStaleMinutes.value = Math.round((Number(cfg.stale_after_s) || 3600) / 60);
    els.presenceDisarmOnArrival.checked = cfg.disarm_on_arrival !== false;
    renderPresenceAutomationNote();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Automation settings failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function savePresenceAutomation() {
  const payload = {
    enabled: els.presenceAutoEnabled.checked,
    arm_away_after_s: Math.max(0, Number(els.presenceArmMinutes.value || 0)) * 60,
    stale_after_s: Math.max(1, Number(els.presenceStaleMinutes.value || 1)) * 60,
    disarm_on_arrival: els.presenceDisarmOnArrival.checked,
  };
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    renderPresenceAutomationNote();
    toast('Automation saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Automation save failed: ' + (exc.message || exc), 'error');
    }
  }
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
  // System-wide AC-power-lost alert (issue #99). Mirrors the low-battery badge:
  // the same aggregate cloud flag, dual-rendered onto Home + Security, clearing
  // when false. Red --deficit tint (vs the battery badge's amber) because the
  // panel running on backup power is more urgent than a single low cell.
  if (security && security.ac_lost) {
    const badge = document.createElement('span');
    badge.className = 'security-aclost-badge';
    badge.textContent = '⚠ AC power lost';
    badge.title = 'The alarm panel lost mains power and is running on backup battery';
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
  renderPresence();
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

// --------------------------------------------------- presence detail + config
function openPresenceDetail(entityId) {
  const entity = presenceById(entityId);
  if (!entity) return;
  state.selectedPresenceId = entityId;
  els.presenceDetailName.textContent = presenceEntityLabel(entity);
  els.presenceDetailStatus.textContent = presenceLabel(entity) + (entity.stale ? ' · stale' : '');
  els.presenceDetailSource.textContent = sourceLabel(entity);
  els.presenceDetailLastSeen.textContent = entity.last_seen ? fmtTime(entity.last_seen) : '—';
  els.presenceDetailDistance.textContent = fmtDistance(entity.distance_from_home_m);
  els.presenceDetailPlace.textContent = placeLabel(entity) || '—';
  renderPresenceMap(entity);
  els.presenceDisplayName.value = entity.display_name || '';
  els.presenceDisplayName.placeholder = entity.name || entity.entity_id || 'Custom label…';
  els.presenceOriginalName.textContent = 'System name: ' + (entity.name || entity.entity_id || 'Unknown');
  renderPresenceHiddenToggle(entity);
  if (typeof els.presenceDialog.showModal === 'function') els.presenceDialog.showModal();
  else els.presenceDialog.setAttribute('open', '');
  els.presenceDisplayName.focus();
}

function mapUrl(entity) {
  if (!coordsKey(entity)) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  return 'https://www.openstreetmap.org/?mlat=' + encodeURIComponent(lat) +
    '&mlon=' + encodeURIComponent(lon) + '#map=16/' + encodeURIComponent(lat) +
    '/' + encodeURIComponent(lon);
}

function mapEmbedUrl(entity) {
  if (!coordsKey(entity)) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  const delta = 0.01;
  const bbox = [lon - delta, lat - delta, lon + delta, lat + delta].join(',');
  return 'https://www.openstreetmap.org/export/embed.html?bbox=' +
    encodeURIComponent(bbox) + '&layer=mapnik&marker=' +
    encodeURIComponent(lat + ',' + lon);
}

function renderPresenceMap(entity) {
  const href = mapUrl(entity);
  if (!href) {
    els.presenceMapLink.hidden = true;
    els.presenceMapFrame.hidden = true;
    els.presenceMapFrame.removeAttribute('src');
    return;
  }
  els.presenceMapLink.hidden = false;
  els.presenceMapLink.href = href;
  els.presenceMapFrame.hidden = false;
  els.presenceMapFrame.src = mapEmbedUrl(entity);
  ensurePlaceLabel(entity);
}

function renderPresenceHiddenToggle(entity) {
  const btn = els.presenceHiddenDetailToggle;
  if (!btn) return;
  const isThisDevice = entity.entity_id === '__this_device__';
  const hidden = !!entity.hidden;
  btn.disabled = isThisDevice;
  btn.title = isThisDevice ? 'This device is always shown when browser location is enabled' : '';
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
    (hidden ? 'ON' : 'OFF') + '</span>';
}

function closePresenceDetail() {
  state.selectedPresenceId = null;
  if (typeof els.presenceDialog.close === 'function') els.presenceDialog.close();
  else els.presenceDialog.removeAttribute('open');
}

async function savePresenceName() {
  if (!state.selectedPresenceId) return;
  const id = state.selectedPresenceId;
  const newName = els.presenceDisplayName.value.trim();
  if (id === '__this_device__') {
    state.thisDevicePresence = Object.assign({}, state.thisDevicePresence, { display_name: newName || null });
    els.presenceDetailName.textContent = presenceEntityLabel(state.thisDevicePresence);
    renderPresence();
    return;
  }
  try {
    await jsonApi('/api/presence/entity-display-name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: id, display_name: newName }),
    });
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id ? Object.assign({}, e, { display_name: newName || null }) : e;
      });
    }
    const entity = presenceById(id);
    if (entity) els.presenceDetailName.textContent = presenceEntityLabel(entity);
    renderPresence();
    toast('Name saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

async function togglePresenceHidden() {
  const id = state.selectedPresenceId;
  if (!id) return;
  if (id === '__this_device__') return;
  const entity = presenceById(id);
  if (!entity) return;
  const next = !entity.hidden;
  try {
    await jsonApi('/api/presence/entity-hidden', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: id, hidden: next }),
    });
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id ? Object.assign({}, e, { hidden: next }) : e;
      });
    }
    renderPresenceHiddenToggle(presenceById(id) || entity);
    renderPresence();
    toast(next ? 'Presence hidden' : 'Presence shown', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to update presence: ' + (exc.message || exc), 'error');
    }
  }
}

function base64UrlToUint8Array(value) {
  const padding = '='.repeat((4 - value.length % 4) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

async function subscribePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    toast('Notifications unavailable in this browser', 'error');
    return;
  }
  try {
    const cfg = await jsonApi('/api/push/config');
    if (!cfg.available || !cfg.public_key) {
      toast('Web Push keys are not configured', 'error');
      return;
    }
    const registration = await navigator.serviceWorker.register('/static/sw.js');
    const existing = await registration.pushManager.getSubscription();
    const sub = existing || await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToUint8Array(cfg.public_key),
    });
    await jsonApi('/api/push/subscriptions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    toast('Notifications enabled', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Notifications failed: ' + (exc.message || exc), 'error');
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

export function wirePresenceControls() {
  try {
    if (localStorage.getItem(PRESENCE_SHOW_HIDDEN_KEY) === 'true') {
      state.presenceShowHidden = true;
    }
  } catch (_) { /* private mode */ }

  if (hydrateThisDeviceLocation()) renderPresence();
  refreshThisDeviceLocation();

  if (els.presenceHiddenToggle) {
    els.presenceHiddenToggle.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      state.presenceShowHidden = !state.presenceShowHidden;
      try {
        localStorage.setItem(PRESENCE_SHOW_HIDDEN_KEY, String(state.presenceShowHidden));
      } catch (_) { /* private mode */ }
      renderPresence();
    });
  }
  if (els.presenceRefresh) {
    els.presenceRefresh.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      refreshPresenceDiagnostics();
    });
  }
  if (els.locationUseBrowser) els.locationUseBrowser.addEventListener('click', useBrowserLocation);
  [els.locationLabel, els.locationLat, els.locationLon].forEach(function (el) {
    if (el) el.addEventListener('blur', saveLocation);
  });
  [els.presenceAutoEnabled, els.presenceArmMinutes, els.presenceStaleMinutes, els.presenceDisarmOnArrival].forEach(function (el) {
    if (el) el.addEventListener('change', savePresenceAutomation);
  });
  if (els.pushSubscribe) els.pushSubscribe.addEventListener('click', subscribePush);

  if (els.presenceDetailClose) els.presenceDetailClose.addEventListener('click', closePresenceDetail);
  if (els.presenceDialog) {
    els.presenceDialog.addEventListener('click', function (ev) {
      if (ev.target === els.presenceDialog) closePresenceDetail();
    });
  }
  if (els.presenceDisplayName) {
    els.presenceDisplayName.addEventListener('blur', savePresenceName);
    els.presenceDisplayName.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); els.presenceDisplayName.blur(); }
    });
  }
  if (els.presenceHiddenDetailToggle) {
    els.presenceHiddenDetailToggle.addEventListener('click', togglePresenceHidden);
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
  if (state.tab === 'security') loadPresence();
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
    if (tab === 'security') {
      loadPresence();
      loadLocation();
      loadPresenceAutomation();
    }
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
