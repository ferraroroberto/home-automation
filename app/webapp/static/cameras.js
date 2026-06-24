/* Cameras tile (Security tab) + detail and full-screen live-view modals.
 *
 * Reads GET /api/cameras; the detail modal shows a fresh snapshot fetched as a
 * blob (an <img> can't carry the bearer header, so we fetch→objectURL); the
 * full-screen view streams MJPEG via <img src=…/stream?token=…> (the one place
 * the token rides the URL, since the middleware accepts ?token=). PTZ is
 * press-and-hold (the server arms a safety auto-stop in case stop is lost).
 * Issue #161. */

'use strict';

import { els, state, toast, readToken, reportFetchOk, reportFetchFailure } from './state.js';
import { api, jsonApi } from './api.js';

let snapshotUrl = null;   // objectURL for the detail-modal snapshot (revoked on replace)
let liveRecording = false;

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

// --- snapshot (blob → objectURL; <img> can't send the bearer header) --------
async function loadSnapshotInto(imgEl, cameraId) {
  imgEl.removeAttribute('src');
  imgEl.classList.add('is-loading');
  try {
    const res = await api('/api/cameras/' + encodeURIComponent(cameraId) + '/snapshot');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    snapshotUrl = URL.createObjectURL(blob);
    imgEl.src = snapshotUrl;
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Snapshot failed: ' + (exc.message || exc), 'error');
    }
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
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Rename failed: ' + (exc.message || exc), 'error');
    }
  }
}

// --- full-screen live view ---------------------------------------------------
function streamUrl(cameraId) {
  const t = readToken();
  return '/api/cameras/' + encodeURIComponent(cameraId) + '/stream' +
    (t ? '?token=' + encodeURIComponent(t) : '');
}

function setRecButton(on) {
  liveRecording = on;
  els.cameraRecBtn.classList.toggle('active', on);
}

function openLiveView(cameraId) {
  const cam = cameraById(cameraId);
  if (!cam) return;
  els.cameraLiveName.textContent = cameraLabel(cam);
  els.cameraLiveImg.src = streamUrl(cameraId);
  setRecButton(!!cam.recording);
  if (typeof els.cameraLiveDialog.showModal === 'function') els.cameraLiveDialog.showModal();
  else els.cameraLiveDialog.setAttribute('open', '');
}

function closeLiveView() {
  if (typeof els.cameraLiveDialog.close === 'function') els.cameraLiveDialog.close();
  else els.cameraLiveDialog.removeAttribute('open');
}

// --- PTZ press-and-hold ------------------------------------------------------
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

function bindHold(btn, payload) {
  if (!btn) return;
  let holding = false;
  const start = function (ev) {
    ev.preventDefault();
    if (holding) return;
    holding = true;
    ptz('start', payload);
  };
  const stop = function () {
    if (!holding) return;
    holding = false;
    ptz('stop', null);
  };
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointerleave', stop);
  btn.addEventListener('pointercancel', stop);
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
  els.cameraDisplayName.addEventListener('blur', saveCameraName);
  els.cameraDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.cameraDisplayName.blur(); }
  });
  els.cameraLiveBtn.addEventListener('click', function () {
    closeCameraDetail();
    if (state.selectedCameraId) openLiveView(state.selectedCameraId);
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
  bindHold(els.cameraPtzUp, { direction: 'up' });
  bindHold(els.cameraPtzDown, { direction: 'down' });
  bindHold(els.cameraPtzLeft, { direction: 'left' });
  bindHold(els.cameraPtzRight, { direction: 'right' });
  bindHold(els.cameraZoomIn, { zoom: 'in' });
  bindHold(els.cameraZoomOut, { zoom: 'out' });
  els.cameraSnapBtn.addEventListener('click', downloadSnapshot);
  els.cameraRecBtn.addEventListener('click', toggleRecord);
}

export function onCamerasTab(tab) {
  if (tab === 'security') loadCameras();
}
