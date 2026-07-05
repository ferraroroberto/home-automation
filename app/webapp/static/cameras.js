/* Cameras tile (Security tab) + detail, live-view, and zoom modals.
 *
 * Reads GET /api/cameras; each list row shows a persisted last-snapshot
 * thumbnail (or a camera glyph when there's none yet) — clicking it zooms the
 * last frame, clicking the name opens the detail modal which grabs a FRESH
 * snapshot (that becomes the new persisted last frame). The full-screen view
 * streams MJPEG via <img src=…/stream?camera_token=…>; thumbnails/zoom use the
 * same short-lived scoped token (issue #261) since an <img> can't carry a header.
 *
 * PTZ has two modes (issue #190): 'step' (one click = one fixed nudge, precise)
 * and 'hold' (press-and-hold continuous move). Cameras that support it also get
 * saved position presets and manual pan/tilt/zoom coordinate entry — both
 * capability-gated, so hardware without them simply doesn't show the controls.
 * Issues #161, #190. */

'use strict';

import { els, state, toast, readToken, reportFetchOk, reportFetchFailure } from './state.js';
import { api, jsonApi } from './api.js';

let snapshotUrl = null;   // objectURL for the detail-modal snapshot (revoked on replace)
let liveRecording = false;
// Cache-bust token per camera for the persisted thumbnail. Seeded once per page
// load (so a tab visit shows the current file) and bumped whenever we grab a
// fresh frame (so the thumbnail updates immediately after open/screenshot).
const snapVersions = {};
const pageNonce = Date.now();

// Short-lived scoped camera token (issue #261): replaces the long-lived bearer
// in <img src> URLs so the bearer never lands in browser history or server logs.
let _camToken = null;
let _camTokenExpiry = 0;

async function _ensureCameraToken() {
  // When no long-lived auth is configured the middleware is open to all — no
  // scoped token is needed and the endpoint would return an empty one anyway.
  if (!readToken()) return;
  if (_camToken && Date.now() < _camTokenExpiry) return;
  try {
    const body = await jsonApi('/api/cameras/stream-token', { method: 'POST' });
    _camToken = body.token || '';
    // Refresh 5 s before expiry so URLs constructed just before the deadline
    // are still valid when they reach the server.
    _camTokenExpiry = body.expires_in > 0
      ? Date.now() + (body.expires_in - 5) * 1000
      : 0;
  } catch (_) {
    _camToken = '';
    _camTokenExpiry = 0;
  }
}

function cameraLabel(cam) {
  return cam.display_name || cam.id;
}

function cameraById(id) {
  return (state.cameras || []).find(function (c) { return c.id === id; }) || null;
}

function cameraStatus(cam) {
  if (!cam.reachable) return 'Offline';
  const parts = [cam.model || 'Camera'];
  if (cam.recording) parts.push('● Recording');
  return parts.join(' · ');
}

function thumbUrl(cameraId) {
  const v = snapVersions[cameraId] || pageNonce;
  return '/api/cameras/' + encodeURIComponent(cameraId) + '/last_snapshot' +
    (_camToken ? '?camera_token=' + encodeURIComponent(_camToken) : '?') + '&v=' + v;
}

// A fresh frame was just persisted → bust the thumbnail cache and re-render.
function bumpSnapshot(cameraId) {
  snapVersions[cameraId] = Date.now();
  renderCameras();
}

const CAMERA_GLYPH = '<svg class="icon camera-thumb-glyph" aria-hidden="true"><use href="#i-camera"></use></svg>';

function buildThumb(cam) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'camera-thumb';
  btn.setAttribute('aria-label', 'Zoom last snapshot ' + cameraLabel(cam));
  if (cam.reachable) {
    const img = document.createElement('img');
    img.className = 'camera-thumb-img';
    img.alt = '';
    img.loading = 'lazy';
    img.src = thumbUrl(cam.id);
    // No persisted frame yet (404) → fall back to the camera glyph.
    img.addEventListener('error', function () {
      btn.classList.add('is-empty');
      btn.innerHTML = CAMERA_GLYPH;
    });
    btn.appendChild(img);
  } else {
    btn.classList.add('is-empty');
    btn.innerHTML = CAMERA_GLYPH;
  }
  btn.addEventListener('click', function () { openZoom(cam.id); });
  return btn;
}

function renderCameras() {
  els.camerasList.innerHTML = '';
  const cameras = state.cameras || [];
  if (!cameras.length) {
    els.camerasNote.hidden = false;
    els.camerasNote.textContent = 'No cameras configured.';
    return;
  }
  els.camerasNote.hidden = true;
  cameras.forEach(function (cam) {
    const row = document.createElement('div');
    row.className = 'security-zone camera-row';
    if (!cam.reachable) row.classList.add('is-bypassed');
    else row.classList.add('is-active');

    row.appendChild(buildThumb(cam));

    const main = document.createElement('div');
    main.className = 'security-zone-main';
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'security-zone-name';
    name.textContent = cameraLabel(cam);
    name.title = 'Camera details · live view · rename';
    name.addEventListener('click', function () { openCameraDetail(cam.id); });
    main.appendChild(name);

    const flags = document.createElement('span');
    flags.className = 'security-zone-flags';
    flags.textContent = cameraStatus(cam);
    main.appendChild(flags);
    row.appendChild(main);

    if (cam.reachable) {
      const live = document.createElement('button');
      live.type = 'button';
      live.className = 'range-tab camera-row-live';
      live.setAttribute('aria-label', 'Open live view ' + cameraLabel(cam));
      live.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-maximize"></use></svg>';
      live.addEventListener('click', function () {
        state.selectedCameraId = cam.id;
        openLiveView(cam.id);
      });
      row.appendChild(live);
    }
    els.camerasList.appendChild(row);
  });
}

export async function loadCameras() {
  await _ensureCameraToken();
  try {
    const body = await jsonApi('/api/cameras');
    state.cameras = (body && body.cameras) || [];
    reportFetchOk('cameras');
    renderCameras();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('cameras', exc, 'cameras');
    state.cameras = [];
    renderCameras();
  }
}

// --- snapshot zoom (the persisted last frame, full size) --------------------
async function openZoom(cameraId) {
  const cam = cameraById(cameraId);
  if (!cam) return;
  await _ensureCameraToken();
  els.cameraZoomName.textContent = cameraLabel(cam);
  els.cameraZoomImg.onerror = function () {
    closeZoom();
    toast('No snapshot yet — open the camera to capture one.', 'warning');
  };
  els.cameraZoomImg.src = thumbUrl(cameraId);
  if (typeof els.cameraZoomDialog.showModal === 'function') els.cameraZoomDialog.showModal();
  else els.cameraZoomDialog.setAttribute('open', '');
}

function closeZoom() {
  els.cameraZoomImg.onerror = null;
  if (typeof els.cameraZoomDialog.close === 'function') els.cameraZoomDialog.close();
  else els.cameraZoomDialog.removeAttribute('open');
}

// --- detail snapshot (blob → objectURL; <img> can't send the bearer header) -
async function loadSnapshotInto(imgEl, cameraId) {
  imgEl.removeAttribute('src');
  imgEl.classList.add('is-loading');
  imgEl.hidden = false;
  if (els.cameraSnapshotEmpty) els.cameraSnapshotEmpty.hidden = true;
  try {
    const res = await api('/api/cameras/' + encodeURIComponent(cameraId) + '/snapshot');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    snapshotUrl = URL.createObjectURL(blob);
    imgEl.src = snapshotUrl;
    // The fresh frame was persisted server-side → refresh the list thumbnail.
    bumpSnapshot(cameraId);
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Snapshot failed: ' + (exc.message || exc), 'error');
    }
    // Styled placeholder instead of a raw broken-image box (issue #362).
    imgEl.hidden = true;
    if (els.cameraSnapshotEmpty) els.cameraSnapshotEmpty.hidden = false;
  } finally {
    imgEl.classList.remove('is-loading');
  }
}

function openCameraDetail(cameraId) {
  const cam = cameraById(cameraId);
  if (!cam) return;
  state.selectedCameraId = cameraId;
  els.cameraDetailName.textContent = cameraLabel(cam);
  els.cameraDetailStatus.textContent = cameraStatus(cam);
  els.cameraDisplayName.value = cam.display_name || '';
  els.cameraDisplayName.placeholder = cam.id;
  els.cameraLiveBtn.hidden = !cam.reachable;
  if (els.cameraSave) els.cameraSave.disabled = true;
  loadSnapshotInto(els.cameraSnapshot, cameraId);
  if (typeof els.cameraDialog.showModal === 'function') els.cameraDialog.showModal();
  else els.cameraDialog.setAttribute('open', '');
}

function closeCameraDetail() {
  if (typeof els.cameraDialog.close === 'function') els.cameraDialog.close();
  else els.cameraDialog.removeAttribute('open');
}

async function saveCameraName() {
  const id = state.selectedCameraId;
  if (!id) return;
  const newName = els.cameraDisplayName.value.trim();
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    state.cameras = (state.cameras || []).map(function (c) {
      return c.id === id ? Object.assign({}, c, { display_name: newName || null }) : c;
    });
    els.cameraDetailName.textContent = cameraLabel(cameraById(id) || { id: id });
    renderCameras();
    if (els.cameraSave) els.cameraSave.disabled = true;
    toast('Saved', 'good');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Rename failed: ' + (exc.message || exc), 'error');
    }
  }
}

// --- full-screen live view ---------------------------------------------------
function streamUrl(cameraId) {
  return '/api/cameras/' + encodeURIComponent(cameraId) + '/stream' +
    (_camToken ? '?camera_token=' + encodeURIComponent(_camToken) : '');
}

function setRecButton(on) {
  liveRecording = on;
  els.cameraRecBtn.classList.toggle('active', on);
}

async function openLiveView(cameraId) {
  const cam = cameraById(cameraId);
  if (!cam) return;
  await _ensureCameraToken();
  els.cameraLiveName.textContent = cameraLabel(cam);
  els.cameraLiveImg.src = streamUrl(cameraId);
  setRecButton(!!cam.recording);
  applyPtzMode();
  // Presets show for native-preset OR absolute-capable cameras; manual
  // coordinates need absolute moves. Hide both for cameras without support.
  const presetsOk = !!(cam.ptz_presets || cam.ptz_absolute);
  els.cameraPresetsRow.hidden = !presetsOk;
  els.cameraCoordsRow.hidden = !cam.ptz_absolute;
  state.cameraPresets = [];
  renderPresets();
  if (presetsOk) loadPresets(cameraId);
  if (cam.ptz_absolute) refreshCoords();
  if (typeof els.cameraLiveDialog.showModal === 'function') els.cameraLiveDialog.showModal();
  else els.cameraLiveDialog.setAttribute('open', '');
}

function closeLiveView() {
  if (typeof els.cameraLiveDialog.close === 'function') els.cameraLiveDialog.close();
  else els.cameraLiveDialog.removeAttribute('open');
}

// --- PTZ: mode toggle + step/hold buttons -----------------------------------
function applyPtzMode() {
  const step = state.cameraPtzMode === 'step';
  els.cameraPtzModeBtn.textContent = step ? 'Step' : 'Hold';
  els.cameraPtzModeBtn.setAttribute('aria-pressed', step ? 'true' : 'false');
  els.cameraPtzModeBtn.title = step
    ? 'One click = one step (precise). Tap to switch to press-and-hold.'
    : 'Press and hold to move. Tap to switch to single-step.';
}

function togglePtzMode() {
  state.cameraPtzMode = state.cameraPtzMode === 'step' ? 'hold' : 'step';
  applyPtzMode();
}

async function ptz(action, payload) {
  const id = state.selectedCameraId;
  if (!id) return;
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/ptz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ action: action }, payload || {})),
    });
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('PTZ failed: ' + (exc.message || exc), 'error');
    }
  }
}

// One handler covers both modes: in 'hold' the button drives a continuous move
// between pointerdown/up; in 'step' a click fires a single fixed nudge.
function bindPtzButton(btn, payload) {
  if (!btn) return;
  let holding = false;
  btn.addEventListener('pointerdown', function (ev) {
    if (state.cameraPtzMode !== 'hold') return;
    ev.preventDefault();
    if (holding) return;
    holding = true;
    ptz('start', payload);
  });
  const stop = function () {
    if (!holding) return;
    holding = false;
    ptz('stop', null);
  };
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointerleave', stop);
  btn.addEventListener('pointercancel', stop);
  btn.addEventListener('click', function () {
    if (state.cameraPtzMode !== 'step') return;
    ptz('step', payload);
  });
}

// --- PTZ presets -------------------------------------------------------------
function renderPresets() {
  const list = els.cameraPresetsList;
  list.innerHTML = '';
  const presets = state.cameraPresets || [];
  if (!presets.length) {
    const empty = document.createElement('span');
    empty.className = 'camera-presets-empty muted';
    empty.textContent = 'None saved';
    list.appendChild(empty);
    return;
  }
  presets.forEach(function (p) {
    const chip = document.createElement('span');
    chip.className = 'camera-preset';
    const go = document.createElement('button');
    go.type = 'button';
    go.className = 'camera-preset-go';
    go.textContent = p.name || p.token;
    go.title = 'Recall ' + (p.name || p.token);
    go.addEventListener('click', function () { gotoPreset(p.token); });
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'camera-preset-rename';
    rename.setAttribute('aria-label', 'Rename ' + (p.name || p.token));
    rename.textContent = '✎';
    rename.addEventListener('click', function () { renamePreset(p.token, p.name || ''); });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'camera-preset-del';
    del.setAttribute('aria-label', 'Delete ' + (p.name || p.token));
    del.textContent = '×';
    del.addEventListener('click', function () { removePreset(p.token); });
    chip.appendChild(go);
    chip.appendChild(rename);
    chip.appendChild(del);
    list.appendChild(chip);
  });
}

async function loadPresets(cameraId) {
  try {
    const body = await jsonApi('/api/cameras/' + encodeURIComponent(cameraId) + '/presets');
    state.cameraPresets = (body && body.presets) || [];
    renderPresets();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      state.cameraPresets = [];
      renderPresets();
    }
  }
}

async function savePreset() {
  const id = state.selectedCameraId;
  if (!id) return;
  // Let the user name the preset so the chip is recognisable instead of an
  // anonymous "Position N" (#212). Cancel aborts; blank falls back to Position N.
  const fallback = 'Position ' + ((state.cameraPresets || []).length + 1);
  const entered = window.prompt('Name this preset:', fallback);
  if (entered === null) return;
  const name = entered.trim() || fallback;
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/presets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name }),
    });
    await loadPresets(id);
    toast('Saved ' + name, 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Save preset failed: ' + (exc.message || exc), 'error');
    }
  }
}

// Rename via a local override (keeps the saved lens position — no re-save/move).
async function renamePreset(token, current) {
  const id = state.selectedCameraId;
  if (!id) return;
  const entered = window.prompt('Rename preset:', current || '');
  if (entered === null) return;
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/presets/' + encodeURIComponent(token) + '/name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: entered.trim() }),
    });
    await loadPresets(id);
    toast('Renamed', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Rename failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function gotoPreset(token) {
  const id = state.selectedCameraId;
  if (!id) return;
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/presets/' +
      encodeURIComponent(token) + '/goto', { method: 'POST' });
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Recall failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function removePreset(token) {
  const id = state.selectedCameraId;
  if (!id) return;
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/presets/' +
      encodeURIComponent(token), { method: 'DELETE' });
    await loadPresets(id);
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Delete failed: ' + (exc.message || exc), 'error');
    }
  }
}

// --- PTZ manual coordinates --------------------------------------------------
function setCoordHint(input, range) {
  if (range && range.length === 2) {
    input.placeholder = range[0] + '…' + range[1];
    input.min = range[0];
    input.max = range[1];
  }
}

async function refreshCoords() {
  const id = state.selectedCameraId;
  if (!id) return;
  try {
    const body = await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/ptz/status');
    if (body.pan != null) els.cameraPanInput.value = Number(body.pan).toFixed(2);
    if (body.tilt != null) els.cameraTiltInput.value = Number(body.tilt).toFixed(2);
    if (body.zoom != null) els.cameraZoomInput.value = Number(body.zoom).toFixed(2);
    setCoordHint(els.cameraPanInput, body.pan_range);
    setCoordHint(els.cameraTiltInput, body.tilt_range);
    setCoordHint(els.cameraZoomInput, body.zoom_range);
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Read position failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function gotoCoords() {
  const id = state.selectedCameraId;
  if (!id) return;
  const pan = parseFloat(els.cameraPanInput.value);
  const tilt = parseFloat(els.cameraTiltInput.value);
  if (Number.isNaN(pan) || Number.isNaN(tilt)) {
    toast('Enter a pan and tilt value', 'warning');
    return;
  }
  const zoomRaw = els.cameraZoomInput.value;
  const payload = { pan: pan, tilt: tilt };
  if (zoomRaw !== '' && !Number.isNaN(parseFloat(zoomRaw))) payload.zoom = parseFloat(zoomRaw);
  try {
    await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/ptz/absolute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Move failed: ' + (exc.message || exc), 'error');
    }
  }
}

// --- snapshot download + record toggle (live view) --------------------------
async function downloadSnapshot() {
  const id = state.selectedCameraId;
  if (!id) return;
  try {
    const res = await api('/api/cameras/' + encodeURIComponent(id) + '/snapshot');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = id + '-' + Date.now() + '.jpg';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    bumpSnapshot(id);   // the grab was persisted as the new last frame
    toast('Screenshot saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Screenshot failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function toggleRecord() {
  const id = state.selectedCameraId;
  if (!id) return;
  const next = !liveRecording;
  try {
    const body = await jsonApi('/api/cameras/' + encodeURIComponent(id) + '/record', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: next ? 'start' : 'stop' }),
    });
    setRecButton(next);
    state.cameras = (state.cameras || []).map(function (c) {
      return c.id === id ? Object.assign({}, c, { recording: next }) : c;
    });
    renderCameras();
    toast(next ? ('Recording → ' + (body.file || 'started')) : 'Recording saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Recording failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireCameras() {
  if (!els.cameraDialog) return;
  els.cameraDetailClose.addEventListener('click', closeCameraDetail);
  els.cameraDialog.addEventListener('click', function (ev) {
    if (ev.target === els.cameraDialog) closeCameraDetail();
  });
  // 'close' fires for the button, backdrop, AND the Esc key — revoke the
  // snapshot blob here so no path leaks it.
  els.cameraDialog.addEventListener('close', function () {
    if (snapshotUrl) { URL.revokeObjectURL(snapshotUrl); snapshotUrl = null; }
  });
  els.cameraDisplayName.addEventListener('input', function () {
    if (els.cameraSave) els.cameraSave.disabled = false;
  });
  if (els.cameraSave) els.cameraSave.addEventListener('click', saveCameraName);
  els.cameraDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveCameraName(); }
  });
  els.cameraLiveBtn.addEventListener('click', function () {
    closeCameraDetail();
    if (state.selectedCameraId) openLiveView(state.selectedCameraId);
  });

  // Zoom lightbox (last persisted frame).
  els.cameraZoomClose.addEventListener('click', closeZoom);
  els.cameraZoomDialog.addEventListener('click', function (ev) {
    if (ev.target === els.cameraZoomDialog) closeZoom();
  });
  els.cameraZoomDialog.addEventListener('close', function () {
    els.cameraZoomImg.removeAttribute('src');
  });

  els.cameraLiveClose.addEventListener('click', closeLiveView);
  els.cameraLiveDialog.addEventListener('click', function (ev) {
    if (ev.target === els.cameraLiveDialog) closeLiveView();
  });
  // 'close' covers the button, backdrop, AND Esc: clearing the <img> src
  // disconnects the MJPEG stream so the server tears down ffmpeg (no leak).
  els.cameraLiveDialog.addEventListener('close', function () {
    els.cameraLiveImg.removeAttribute('src');
  });
  bindPtzButton(els.cameraPtzUp, { direction: 'up' });
  bindPtzButton(els.cameraPtzDown, { direction: 'down' });
  bindPtzButton(els.cameraPtzLeft, { direction: 'left' });
  bindPtzButton(els.cameraPtzRight, { direction: 'right' });
  bindPtzButton(els.cameraZoomIn, { zoom: 'in' });
  bindPtzButton(els.cameraZoomOut, { zoom: 'out' });
  els.cameraPtzModeBtn.addEventListener('click', togglePtzMode);
  els.cameraPresetSave.addEventListener('click', savePreset);
  els.cameraCoordsRefresh.addEventListener('click', refreshCoords);
  els.cameraCoordsGo.addEventListener('click', gotoCoords);
  els.cameraSnapBtn.addEventListener('click', downloadSnapshot);
  els.cameraRecBtn.addEventListener('click', toggleRecord);
  applyPtzMode();
}

export function onCamerasTab(tab) {
  if (tab === 'security') loadCameras();
}
