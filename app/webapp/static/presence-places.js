/* Named-places dense-collection editor for the "where's mom/dad" voice locator
 * (issue #438). Same summary-row + staged-dialog contract as
 * ./security-schedules.js (list-row summary → Edit opens a staged <dialog>;
 * Save is the only persistence boundary; the whole list is PUT back).
 *
 * The "Pick on map" flow adds a second, stacked <dialog> with a vendored
 * Leaflet map (static/vendor/leaflet/) so a place doesn't have to be typed
 * as raw coordinates or be wherever the phone currently is.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';
import { confirmAction } from './network.js';

const DEFAULT_RADIUS_M = 150;

let editorIndex = null;
let editorPlaceId = null;
let editorReturnFocus = null;
let stagedPlace = null;

let mapPicker = null; // { map, marker } — created lazily on first "Pick on map".

function placeDefaults() {
  return {
    id: 'place-' + Date.now().toString(36),
    label: '',
    lat: 0,
    lon: 0,
    radius_m: DEFAULT_RADIUS_M,
  };
}

function normalizedPlaces(entries) {
  return (entries || state.presencePlacesList || []).map(function (entry, idx) {
    return {
      id: entry.id || ('place-' + (idx + 1)),
      label: entry.label || ('place-' + (idx + 1)),
      lat: Number(entry.lat) || 0,
      lon: Number(entry.lon) || 0,
      radius_m: Number(entry.radius_m) || DEFAULT_RADIUS_M,
    };
  });
}

function fmtRadius(radiusM) {
  const n = Number(radiusM) || DEFAULT_RADIUS_M;
  return n >= 1000 ? (n / 1000).toFixed(1) + ' km radius' : Math.round(n) + ' m radius';
}

export function renderPresencePlaces() {
  if (!els.presencePlacesList || !els.presencePlacesNote) return;
  els.presencePlacesList.innerHTML = '';
  state.presencePlacesList = normalizedPlaces();
  if (!state.presencePlacesList.length) {
    els.presencePlacesNote.hidden = false;
    els.presencePlacesNote.textContent = 'No places configured yet.';
    return;
  }
  els.presencePlacesNote.hidden = true;

  state.presencePlacesList.forEach(function (entry, idx) {
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.placeId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit place ' + entry.label);

    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = entry.label;
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = fmtRadius(entry.radius_m);
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);
    main.addEventListener('click', function () { openPlaceEditor(idx, main); });
    row.appendChild(main);
    els.presencePlacesList.appendChild(row);
  });
}

export async function loadPresencePlaces() {
  if (!els.presencePlacesList) return;
  try {
    const body = await jsonApi('/api/presence/places');
    state.presencePlacesList = (body && body.places) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.presencePlacesList = [];
    if (els.presencePlacesNote) {
      els.presencePlacesNote.hidden = false;
      els.presencePlacesNote.textContent = exc.message || 'Failed to load places.';
    }
  }
  renderPresencePlaces();
}

async function savePresencePlaces(entries) {
  const previous = state.presencePlacesList;
  state.presencePlacesList = normalizedPlaces(entries);
  renderPresencePlaces();
  try {
    const body = await jsonApi('/api/presence/places', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ places: state.presencePlacesList }),
    });
    state.presencePlacesList = (body && body.places) || [];
    renderPresencePlaces();
    toast('Places saved', 'success');
    return true;
  } catch (exc) {
    state.presencePlacesList = previous;
    renderPresencePlaces();
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save places", 'error');
    }
    return false;
  }
}

function openPlaceEditor(index, trigger) {
  editorIndex = index;
  const source = index == null ? placeDefaults() : state.presencePlacesList[index];
  stagedPlace = {
    id: source.id,
    label: source.label,
    lat: source.lat,
    lon: source.lon,
    radius_m: source.radius_m,
  };
  editorPlaceId = stagedPlace.id;
  editorReturnFocus = trigger || null;
  els.presencePlaceEditorTitle.textContent = index == null ? 'Add place' : 'Edit place';
  els.presencePlaceLabel.value = stagedPlace.label;
  els.presencePlaceLat.value = stagedPlace.lat || '';
  els.presencePlaceLon.value = stagedPlace.lon || '';
  els.presencePlaceRadius.value = stagedPlace.radius_m;
  els.presencePlaceDelete.hidden = index == null;
  if (typeof els.presencePlaceDialog.showModal === 'function') els.presencePlaceDialog.showModal();
  else els.presencePlaceDialog.setAttribute('open', '');
  els.presencePlaceLabel.focus();
}

function closePlaceEditor() {
  if (typeof els.presencePlaceDialog.close === 'function') els.presencePlaceDialog.close();
  else els.presencePlaceDialog.removeAttribute('open');
}

function restoreEditorFocus() {
  let target = editorReturnFocus && editorReturnFocus.isConnected ? editorReturnFocus : null;
  if (!target && editorPlaceId) {
    const row = els.presencePlacesList.querySelector('[data-place-id="' + CSS.escape(editorPlaceId) + '"]');
    if (row) target = row.querySelector('.automation-summary-main');
  }
  if (!target) target = els.presencePlaceAdd;
  editorIndex = null;
  editorPlaceId = null;
  editorReturnFocus = null;
  stagedPlace = null;
  if (target) requestAnimationFrame(function () { target.focus(); });
}

function useBrowserLocationForPlace() {
  if (!navigator.geolocation) {
    toast('Location unavailable in this browser', 'error');
    return;
  }
  navigator.geolocation.getCurrentPosition(function (pos) {
    els.presencePlaceLat.value = pos.coords.latitude.toFixed(6);
    els.presencePlaceLon.value = pos.coords.longitude.toFixed(6);
  }, function (err) {
    toast('Location failed: ' + err.message, 'error');
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
}

// ------------------------------------------------------------- map picker

const PIN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 4.993-5.539 10.193-7.399 ' +
  '11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/></svg>';

function mapPickerCenter() {
  const lat = Number(els.presencePlaceLat.value);
  const lon = Number(els.presencePlaceLon.value);
  if (Number.isFinite(lat) && Number.isFinite(lon) && (lat !== 0 || lon !== 0)) return [lat, lon];
  const home = state.location || {};
  if (Number.isFinite(Number(home.lat)) && Number.isFinite(Number(home.lon)) && (home.lat || home.lon)) {
    return [Number(home.lat), Number(home.lon)];
  }
  return [0, 0];
}

function renderMapPickerCoords(latlng) {
  if (!els.presenceMapPickerCoords) return;
  els.presenceMapPickerCoords.textContent = latlng.lat.toFixed(6) + ', ' + latlng.lng.toFixed(6);
}

function ensureMapPicker() {
  if (mapPicker || typeof window.L === 'undefined' || !els.presenceMapPicker) return mapPicker;
  const map = window.L.map(els.presenceMapPicker, { zoomControl: true });
  window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  const icon = window.L.divIcon({
    className: 'place-map-pin',
    html: PIN_SVG,
    iconSize: [32, 32],
    iconAnchor: [16, 30],
  });
  const marker = window.L.marker([0, 0], { icon: icon, draggable: true }).addTo(map);
  marker.on('drag', function (ev) { renderMapPickerCoords(ev.target.getLatLng()); });
  map.on('click', function (ev) {
    marker.setLatLng(ev.latlng);
    renderMapPickerCoords(ev.latlng);
  });
  mapPicker = { map: map, marker: marker };
  return mapPicker;
}

function openMapPicker() {
  const picker = ensureMapPicker();
  if (!picker) {
    toast('Map unavailable', 'error');
    return;
  }
  const center = mapPickerCenter();
  const zoom = center[0] === 0 && center[1] === 0 ? 2 : 15;
  if (typeof els.presenceMapPickerDialog.showModal === 'function') els.presenceMapPickerDialog.showModal();
  else els.presenceMapPickerDialog.setAttribute('open', '');
  requestAnimationFrame(function () {
    picker.map.invalidateSize();
    picker.map.setView(center, zoom);
    picker.marker.setLatLng(center);
    renderMapPickerCoords({ lat: center[0], lng: center[1] });
  });
}

function closeMapPicker() {
  if (typeof els.presenceMapPickerDialog.close === 'function') els.presenceMapPickerDialog.close();
  else els.presenceMapPickerDialog.removeAttribute('open');
}

async function confirmMapPicker() {
  if (!mapPicker) return;
  const latlng = mapPicker.marker.getLatLng();
  els.presencePlaceLat.value = latlng.lat.toFixed(6);
  els.presencePlaceLon.value = latlng.lng.toFixed(6);
  closeMapPicker();
  if (!els.presencePlaceLabel.value.trim()) {
    try {
      const body = await jsonApi(
        '/api/location/reverse?lat=' + encodeURIComponent(latlng.lat) + '&lon=' + encodeURIComponent(latlng.lng)
      );
      if (body && body.available && body.label) els.presencePlaceLabel.value = body.label;
    } catch (_) { /* best-effort label suggestion only */ }
  }
}

export function wirePresencePlaces() {
  if (!els.presencePlaceAdd || !els.presencePlaceDialog) return;
  els.presencePlaceAdd.addEventListener('click', function () {
    openPlaceEditor(null, els.presencePlaceAdd);
  });
  els.presencePlaceEditorClose.addEventListener('click', closePlaceEditor);
  els.presencePlaceDialog.addEventListener('click', function (ev) {
    if (ev.target === els.presencePlaceDialog) closePlaceEditor();
  });
  els.presencePlaceDialog.addEventListener('close', restoreEditorFocus);
  if (els.presencePlaceUseBrowser) els.presencePlaceUseBrowser.addEventListener('click', useBrowserLocationForPlace);
  if (els.presencePlacePickMap) els.presencePlacePickMap.addEventListener('click', openMapPicker);

  els.presencePlaceSave.addEventListener('click', async function () {
    if (!stagedPlace) return;
    stagedPlace.label = (els.presencePlaceLabel.value || '').trim() || stagedPlace.id;
    stagedPlace.lat = Number(els.presencePlaceLat.value) || 0;
    stagedPlace.lon = Number(els.presencePlaceLon.value) || 0;
    stagedPlace.radius_m = Math.max(10, Number(els.presencePlaceRadius.value) || DEFAULT_RADIUS_M);
    const proposed = state.presencePlacesList.slice();
    if (editorIndex == null) proposed.push(stagedPlace);
    else proposed[editorIndex] = stagedPlace;
    els.presencePlaceSave.disabled = true;
    const saved = await savePresencePlaces(proposed);
    els.presencePlaceSave.disabled = false;
    if (saved) closePlaceEditor();
  });
  els.presencePlaceDelete.addEventListener('click', async function () {
    if (editorIndex == null) return;
    const ok = await confirmAction({
      title: 'Delete this place?',
      message: 'This named place will be removed permanently.',
      okLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const proposed = state.presencePlacesList.filter(function (_entry, idx) { return idx !== editorIndex; });
    if (await savePresencePlaces(proposed)) closePlaceEditor();
  });

  if (els.presenceMapPickerClose) els.presenceMapPickerClose.addEventListener('click', closeMapPicker);
  if (els.presenceMapPickerDialog) {
    els.presenceMapPickerDialog.addEventListener('click', function (ev) {
      if (ev.target === els.presenceMapPickerDialog) closeMapPicker();
    });
  }
  if (els.presenceMapPickerConfirm) els.presenceMapPickerConfirm.addEventListener('click', confirmMapPicker);
}
