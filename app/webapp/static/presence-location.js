/* Presence — home-location editor + "this device" browser-GPS tracking
 * (split out of ./presence.js, issue #454 maintainability split).
 *
 * Owns GET/PUT /api/location, the browser geolocation capture that builds the
 * synthetic "__this_device__" presence entity, and its localStorage persistence
 * (so the browser tab keeps reporting after a reload without asking again).
 * Calls back into ./presence.js's renderPresence()/loadPresence() after a write
 * so the card reflects the new state — a two-way import already established by
 * the network.js ↔ network-devices.js precedent (#197).
 */

'use strict';

import {
  state,
  els,
  toast,
  THIS_DEVICE_PRESENCE_KEY,
  THIS_DEVICE_LOCATION_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import { renderPresence, loadPresence } from './presence.js';

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

export function hydrateThisDeviceLocation() {
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

export function updateThisDeviceFromPosition(pos) {
  const lat = pos.coords.latitude;
  const lon = pos.coords.longitude;
  const lastSeen = new Date().toISOString();
  const accuracy = pos.coords.accuracy || null;
  state.thisDevicePresence = buildThisDevicePresence(lat, lon, accuracy, lastSeen);
  storeThisDeviceLocation(lat, lon, accuracy, lastSeen);
  renderPresence();
}

export function refreshThisDeviceLocation() {
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

export function wirePresenceLocationControls() {
  if (els.locationUseBrowser) els.locationUseBrowser.addEventListener('click', useBrowserLocation);
  [els.locationLabel, els.locationLat, els.locationLon].forEach(function (el) {
    if (el) el.addEventListener('blur', saveLocation);
  });
}
