/* Presence card controller (split out of security.js, issue #197).
 *
 * Owns the people list, hide/rename, the home-location editor, the alarm
 * automation knobs, and Web Push enrolment. Reads through GET /api/presence,
 * /api/location and /api/presence/automation; writes are PUT/POST calls that
 * re-render from the returned live state. This is a leaf module: it depends
 * only on ./state.js and ./api.js, so other security sub-modules may import its
 * shared formatter (fmtTime) without creating a cycle.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
  PRESENCE_SHOW_HIDDEN_KEY,
  THIS_DEVICE_PRESENCE_KEY,
  THIS_DEVICE_LOCATION_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import { emptyStateEl } from './empty-state.js';
import { toggleMarkup, setToggleState, isToggleOn, wireToggle } from './toggle.js';

let presenceViewState = 'idle';
let presenceUpdatedAt = null;
let presenceTransportUnavailable = false;

export function fmtTime(value) {
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

function renderKidsHomeToggle() {
  if (!els.presenceKidsHome) return;
  const on = !!(state.presence && state.presence.kids_home_override);
  els.presenceKidsHome.classList.toggle('active', on);
  els.presenceKidsHome.setAttribute('aria-pressed', on ? 'true' : 'false');
  els.presenceKidsHome.disabled = presenceViewState !== 'ready';
}

function setPresenceViewState(next, opts) {
  presenceViewState = next;
  if (opts && opts.updatedAt) presenceUpdatedAt = opts.updatedAt;
  if (opts && Object.prototype.hasOwnProperty.call(opts, 'transportUnavailable')) {
    presenceTransportUnavailable = opts.transportUnavailable;
  }
}

function lastUpdatedLabel() {
  const updated = presenceUpdatedAt instanceof Date
    ? presenceUpdatedAt
    : new Date(presenceUpdatedAt || '');
  if (Number.isNaN(updated.getTime())) return 'Last updated earlier';
  return 'Last updated ' + updated.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function showPresenceState(message, retry) {
  const options = retry ? {
    actionLabel: 'Retry',
    onAction: function () { loadPresence(); },
  } : null;
  els.presenceList.appendChild(emptyStateEl('smartphone', message, options));
}

function hidePresenceRefreshNote() {
  if (els.presenceRefreshNote) els.presenceRefreshNote.hidden = true;
}

function markPresenceFailure() {
  const hasLastGood = !!(state.presence && state.presence.available !== false);
  setPresenceViewState(hasLastGood ? 'stale' : 'error', {
    transportUnavailable: true,
  });
  reportFetchFailure(
    'presence',
    { message: 'live data unavailable' },
    'presence'
  );
  renderPresence();
}

// "Where's mom/dad" Home-tab locator (issue #438) — derives from the same
// state.presence entities the Security-tab Presence card already polls, so
// there is no separate fetch/poll cadence for this card.
function renderLocator() {
  if (!els.locatorList) return;
  els.locatorList.innerHTML = '';
  if (presenceViewState === 'loading' && !state.presence) {
    els.locatorList.appendChild(emptyStateEl('map-pin', 'Reading locations…'));
    return;
  }
  const presence = state.presence;
  if (!presence) {
    els.locatorList.appendChild(emptyStateEl('map-pin', 'Locator unavailable'));
    return;
  }
  const visible = (presence.entities || []).filter(function (e) { return !e.hidden; });
  if (!visible.length) {
    els.locatorList.appendChild(emptyStateEl(
      'map-pin',
      "No tracked devices yet — they appear in the Security tab's Presence card"
    ));
    return;
  }
  visible
    .slice()
    .sort(function (a, b) {
      if (!!a.role !== !!b.role) return a.role ? -1 : 1;
      const ka = a.role || presenceEntityLabel(a);
      const kb = b.role || presenceEntityLabel(b);
      return ka.localeCompare(kb, undefined, { sensitivity: 'base' });
    })
    .forEach(function (entity) {
      const row = document.createElement('div');
      row.className = 'locator-row';

      const main = document.createElement('span');
      main.className = 'locator-main';
      const name = document.createElement('span');
      name.className = 'locator-name';
      name.textContent = presenceEntityLabel(entity);
      main.appendChild(name);
      if (entity.role) {
        const role = document.createElement('span');
        role.className = 'locator-role muted small';
        role.textContent = entity.role;
        main.appendChild(role);
      }
      row.appendChild(main);

      const status = document.createElement('span');
      status.className = 'locator-status';
      const place = document.createElement('span');
      place.className = 'locator-place';
      place.textContent = entity.current_place || 'Unknown';
      status.appendChild(place);
      if (entity.last_seen) {
        const seen = document.createElement('span');
        seen.className = 'locator-meta muted small';
        seen.textContent = fmtTime(entity.last_seen);
        status.appendChild(seen);
      }
      row.appendChild(status);

      els.locatorList.appendChild(row);
    });
}

export function renderPresence() {
  renderLocator();
  renderKidsHomeToggle();
  if (!els.presenceSummary || !els.presenceList || !els.presenceNote) return;
  els.presenceList.innerHTML = '';
  els.presenceList.dataset.state = presenceViewState;
  els.presenceList.setAttribute(
    'aria-busy',
    presenceViewState === 'loading' ? 'true' : 'false'
  );
  const presence = state.presence;
  if (presenceViewState === 'loading') {
    els.presenceSummary.textContent = 'Loading';
    showPresenceState('Reading presence…', false);
    els.presenceNote.hidden = true;
    hidePresenceRefreshNote();
    return;
  }
  if (presenceViewState === 'error' && presenceTransportUnavailable) {
    els.presenceSummary.textContent = 'Unavailable';
    showPresenceState('Presence unavailable', true);
    els.presenceNote.hidden = false;
    els.presenceNote.textContent =
      'Live presence data is unavailable. Check the connection, then retry.';
    hidePresenceRefreshNote();
    return;
  }
  if (!presence) {
    els.presenceSummary.textContent = '—';
    els.presenceNote.hidden = true;
    return;
  }
  if (presence.available === false) {
    els.presenceSummary.textContent = 'Unavailable';
    showPresenceState('Presence unavailable', false);
    els.presenceNote.hidden = false;
    els.presenceNote.textContent = presence.reason === '2fa_required'
      ? 'iCloud needs 2FA; run the CLI once to refresh the trusted session.'
      : (presence.detail || 'Presence is not configured.');
    hidePresenceRefreshNote();
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

  if (!entities.length) {
    showPresenceState('No presence entities configured', false);
    els.presenceNote.hidden = true;
    renderPresenceRefreshNote();
    renderPresenceAutomationNote();
    return;
  }

  if (presenceViewState === 'stale') {
    els.presenceNote.hidden = false;
    els.presenceNote.textContent = lastUpdatedLabel() + ' · live data unavailable';
  } else {
    els.presenceNote.hidden = visible.length > 0;
    els.presenceNote.textContent = visible.length ? '' : 'No presence entities shown.';
  }

  if (!visible.length) showPresenceState('No presence entities shown', false);

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
  els.presenceRefreshNote.hidden = false;
  const diag = (state.presence && state.presence.diagnostics) || {};
  const interval = Math.max(1, Math.round((Number(diag.refresh_interval_s) || 300) / 60));
  const last = diag.refreshed_at ? fmtTime(diag.refreshed_at) : 'not yet';
  els.presenceRefreshNote.textContent =
    'The open tab reloads the local snapshot every 10 s. Find My refreshes in the background about every ' + interval +
    ' min. This device is browser GPS only: it updates only while this tab/PWA is open and is not used for alarm automation. Alarm automation uses Shortcut webhook people. Last Find My refresh: ' + last + '.';
}

function renderPresenceAutomationNote() {
  if (!els.presenceAutomationNote || !els.presenceAutoEnabled) return;
  const entities = (state.presence && state.presence.entities) || [];
  const hasWebhookPerson = entities.some(function (entity) {
    return entity.source === 'webhook' && !entity.hidden;
  });
  if (isToggleOn(els.presenceAutoEnabled) && !hasWebhookPerson) {
    els.presenceAutomationNote.textContent = 'Configure iOS Shortcut arrive/leave webhooks before enabling alarm automation. Browser GPS and Find My diagnostics do not drive arm/disarm.';
    els.presenceAutomationNote.hidden = false;
  } else {
    els.presenceAutomationNote.hidden = true;
    els.presenceAutomationNote.textContent = '';
  }
}

export async function loadPresence() {
  if (!state.presence) {
    setPresenceViewState('loading', { transportUnavailable: false });
    renderPresence();
  }
  try {
    state.presence = await jsonApi('/api/presence');
    reportFetchOk('presence');
    const entities = (state.presence && state.presence.entities) || [];
    const hasEntities = entities.length > 0 || !!state.thisDevicePresence;
    setPresenceViewState(
      state.presence && state.presence.available === false
        ? 'error'
        : (hasEntities ? 'ready' : 'empty'),
      { updatedAt: new Date(), transportUnavailable: false }
    );
    refreshThisDeviceLocation();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    markPresenceFailure();
    return;
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

// Re-fetch Find My diagnostics on demand. No longer wired to a button (the
// background refresher owns the cadence); still called after a home-location
// change so the new distances appear immediately.
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

// "Kids home" override: when on, the everyone-away webhook arms perimeter
// instead of full. Auto-resets server-side on the next disarm-on-arrival.
async function toggleKidsHome() {
  const next = !(state.presence && state.presence.kids_home_override);
  try {
    await jsonApi('/api/presence/kids_home_override', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: next }),
    });
    await loadPresence();
    toast(next ? 'Kids home on · perimeter when away' : 'Kids home off', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Kids home toggle failed: ' + (exc.message || exc), 'error');
    }
  }
}

export async function loadLocation() {
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

export async function loadPresenceAutomation() {
  if (!els.presenceAutoEnabled) return;
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation');
    const cfg = state.presenceAutomation || {};
    setToggleState(els.presenceAutoEnabled, cfg.enabled === true);
    els.presenceArmMinutes.value = Math.round((Number(cfg.arm_away_after_s) || 0) / 60);
    els.presenceStaleMinutes.value = Math.round((Number(cfg.stale_after_s) || 3600) / 60);
    setToggleState(els.presenceDisarmOnArrival, cfg.disarm_on_arrival !== false);
    renderPresenceAutomationNote();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Automation settings failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function savePresenceAutomation() {
  const payload = {
    enabled: isToggleOn(els.presenceAutoEnabled),
    arm_away_after_s: Math.max(0, Number(els.presenceArmMinutes.value || 0)) * 60,
    stale_after_s: Math.max(1, Number(els.presenceStaleMinutes.value || 1)) * 60,
    disarm_on_arrival: isToggleOn(els.presenceDisarmOnArrival),
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
  if (els.presenceRole) {
    els.presenceRole.value = entity.role || '';
    els.presenceRole.disabled = isThisDevice(entity);
  }
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
  btn.innerHTML = toggleMarkup(hidden);
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

async function savePresenceRole() {
  if (!state.selectedPresenceId || state.selectedPresenceId === '__this_device__') return;
  const id = state.selectedPresenceId;
  const newRole = els.presenceRole.value.trim();
  try {
    await jsonApi('/api/presence/role', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: id, role: newRole }),
    });
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id ? Object.assign({}, e, { role: newRole || null }) : e;
      });
    }
    toast('Role saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save role: ' + (exc.message || exc), 'error');
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
  if (els.presenceKidsHome) {
    // The button lives in the <summary>, so swallow the click to toggle the
    // override instead of collapsing the card.
    els.presenceKidsHome.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      toggleKidsHome();
    });
  }
  if (els.locationUseBrowser) els.locationUseBrowser.addEventListener('click', useBrowserLocation);
  [els.locationLabel, els.locationLat, els.locationLon].forEach(function (el) {
    if (el) el.addEventListener('blur', saveLocation);
  });
  [els.presenceArmMinutes, els.presenceStaleMinutes].forEach(function (el) {
    if (el) el.addEventListener('change', savePresenceAutomation);
  });
  wireToggle(els.presenceAutoEnabled, savePresenceAutomation);
  wireToggle(els.presenceDisarmOnArrival, savePresenceAutomation);
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
  if (els.presenceRole) {
    els.presenceRole.addEventListener('blur', savePresenceRole);
    els.presenceRole.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); els.presenceRole.blur(); }
    });
  }
  if (els.presenceHiddenDetailToggle) {
    els.presenceHiddenDetailToggle.addEventListener('click', togglePresenceHidden);
  }
}
