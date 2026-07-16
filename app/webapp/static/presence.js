/* Presence card controller — boot/core (split out of security.js, issue #197;
 * further split, issue #454).
 *
 * Owns the people list (render, hide, rename), the detail modal, and the
 * "where's mom/dad" locator card — this is a leaf-ish module: it depends only
 * on ./state.js and ./api.js plus its three feature sibling modules (issue
 * #454 maintainability split, mirroring the network.js → network-devices/
 * wifi/dhcp.js boot + feature-module pattern):
 *   ./presence-location.js    home-location editor + "this device" browser GPS
 *   ./presence-automation.js  alarm-automation knobs (arm delay, kids-home)
 *   ./presence-push.js        Web Push enrolment
 * Reads through GET /api/presence, /api/location and /api/presence/automation
 * (the latter two owned by the sub-modules); writes are PUT/POST calls that
 * re-render from the returned live state. Other security sub-modules may
 * import the shared formatter (fmtTime) without creating a cycle.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
  PRESENCE_SHOW_HIDDEN_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import { emptyStateEl } from './empty-state.js';
import { toggleMarkup } from './toggle.js';
import { hydrateThisDeviceLocation, refreshThisDeviceLocation, wirePresenceLocationControls } from './presence-location.js';
import { renderKidsHomeToggle, renderPresenceAutomationNote, wirePresenceAutomationControls } from './presence-automation.js';
import { wirePresencePushControls } from './presence-push.js';

// Re-export so callers (security.js, main.js) keep a single import surface —
// same convention security.js itself uses for its own sub-modules.
export { loadLocation } from './presence-location.js';
export { loadPresenceAutomation } from './presence-automation.js';

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

// Fail loud when the Find My diagnostics source itself is broken (#442) —
// distinct from a person simply being "away, unknown exact location".
const LOCATOR_BROKEN_SOURCE_REASONS = ['error', '2fa_required', 'not_configured'];
function renderLocatorSourceNote() {
  if (!els.locatorSourceNote) return;
  const diag = (state.presence && state.presence.diagnostics) || {};
  const broken = diag.available === false && LOCATOR_BROKEN_SOURCE_REASONS.indexOf(diag.reason) !== -1;
  if (!broken) {
    els.locatorSourceNote.hidden = true;
    els.locatorSourceNote.textContent = '';
    return;
  }
  els.locatorSourceNote.textContent = diag.reason === '2fa_required'
    ? 'Find My needs iCloud re-authentication (2FA).'
    : 'Find My location tracking is down — needs re-authentication.';
  els.locatorSourceNote.hidden = false;
}

// "Where's mom/dad" Home-tab locator (issue #438) — derives from the same
// state.presence entities the Security-tab Presence card already polls, so
// there is no separate fetch/poll cadence for this card.
function renderLocator() {
  if (!els.locatorList) return;
  renderLocatorSourceNote();
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
  // Only entities with a role alias appear here — a role is what makes "where's
  // dad/mom" answerable at all, and it doubles as an explicit opt-in so the
  // card doesn't list every tracked device/person by default (#442).
  const visible = (presence.entities || []).filter(function (e) { return !e.hidden && e.role; });
  if (!visible.length) {
    els.locatorList.appendChild(emptyStateEl(
      'map-pin',
      'No one has a role yet — set one (e.g. "dad", "mom") in a person’s detail modal'
    ));
    return;
  }
  visible
    .slice()
    .sort(function (a, b) { return a.role.localeCompare(b.role, undefined, { sensitivity: 'base' }); })
    .forEach(function (entity) {
      const row = document.createElement('div');
      row.className = 'locator-row';

      const main = document.createElement('span');
      main.className = 'locator-main';
      const name = document.createElement('span');
      name.className = 'locator-name';
      name.textContent = presenceEntityLabel(entity);
      main.appendChild(name);
      const role = document.createElement('span');
      role.className = 'locator-role muted small';
      role.textContent = entity.role;
      main.appendChild(role);
      row.appendChild(main);

      const status = document.createElement('span');
      status.className = 'locator-status';
      const place = document.createElement('span');
      place.className = 'locator-place';
      place.textContent = entity.current_place === 'Away'
        ? (placeLabel(entity) || 'Away')
        : (entity.current_place || 'Unknown');
      status.appendChild(place);
      ensurePlaceLabel(entity);
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
  renderKidsHomeToggle(presenceViewState === 'ready');
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
  if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
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

async function savePresenceDetail() {
  if (!state.selectedPresenceId) return;
  const id = state.selectedPresenceId;
  const newName = els.presenceDisplayName.value.trim();
  if (id === '__this_device__') {
    state.thisDevicePresence = Object.assign({}, state.thisDevicePresence, { display_name: newName || null });
    els.presenceDetailName.textContent = presenceEntityLabel(state.thisDevicePresence);
    renderPresence();
    if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
    return;
  }
  const newRole = els.presenceRole ? els.presenceRole.value.trim() : '';
  try {
    await Promise.all([
      jsonApi('/api/presence/entity-display-name', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: id, display_name: newName }),
      }),
      jsonApi('/api/presence/role', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: id, role: newRole }),
      }),
    ]);
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id
          ? Object.assign({}, e, { display_name: newName || null, role: newRole || null })
          : e;
      });
    }
    const entity = presenceById(id);
    if (entity) els.presenceDetailName.textContent = presenceEntityLabel(entity);
    renderPresence();
    if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
    toast('Saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save: ' + (exc.message || exc), 'error');
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
  wirePresenceAutomationControls();
  wirePresenceLocationControls();
  wirePresencePushControls();

  if (els.presenceDetailClose) els.presenceDetailClose.addEventListener('click', closePresenceDetail);
  if (els.presenceDialog) {
    els.presenceDialog.addEventListener('click', function (ev) {
      if (ev.target === els.presenceDialog) closePresenceDetail();
    });
  }
  [els.presenceDisplayName, els.presenceRole].forEach(function (el) {
    if (!el) return;
    el.addEventListener('input', function () {
      if (els.presenceDetailSave) els.presenceDetailSave.disabled = false;
    });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); savePresenceDetail(); }
    });
  });
  if (els.presenceDetailSave) els.presenceDetailSave.addEventListener('click', savePresenceDetail);
  if (els.presenceHiddenDetailToggle) {
    els.presenceHiddenDetailToggle.addEventListener('click', togglePresenceHidden);
  }
}
