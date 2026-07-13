/* Alarm-scene capture pairings editor (issue #162).
 *
 * Maps each RISCO detector to the camera(s) + PTZ preset that should be
 * snapshotted and AI-analysed when that detector trips the alarm. Mirrors the
 * schedule editor's load/normalise/render/save shape (security-schedules.js),
 * persisting through GET/PUT /api/security/scene-pairings.
 *
 * Detector options come from the already-loaded security state (state.security
 * .zones); camera options from GET /api/cameras; preset options are fetched
 * per-camera on demand and cached. A pairing needs a detector + a camera; the
 * preset is optional (no preset = snapshot wherever the lens already points).
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { detectorOptions } from './security-shared.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { confirmAction } from './network.js';

// cameraId -> [{token, name}], fetched lazily so we don't hit every camera up front.
const presetCache = {};
const presetLoading = {};
let editorIndex = null;
let editorPairingId = null;
let editorReturnFocus = null;
let stagedPairing = null;

function pairingDefaults() {
  return {
    id: 'pairing-' + Date.now().toString(36),
    enabled: true,
    zone_id: null,
    camera_id: '',
    preset_token: null,
    preset_name: null,
  };
}

function normalizedPairings(entries) {
  return (entries || state.scenePairings || []).map(function (entry, idx) {
    const zoneId = Number(entry.zone_id);
    return {
      id: entry.id || ('pairing-' + (idx + 1)),
      enabled: entry.enabled !== false,
      zone_id: Number.isFinite(zoneId) ? zoneId : null,
      camera_id: String(entry.camera_id || ''),
      preset_token: entry.preset_token || null,
      preset_name: entry.preset_name || null,
    };
  });
}

async function fetchPresets(cameraId) {
  if (!cameraId) return [];
  if (presetCache[cameraId]) return presetCache[cameraId];
  if (!presetLoading[cameraId]) {
    presetLoading[cameraId] = jsonApi('/api/cameras/' + encodeURIComponent(cameraId) + '/presets')
      .then(function (body) {
        presetCache[cameraId] = (body && body.presets) || [];
        return presetCache[cameraId];
      })
      .catch(function () {
        presetCache[cameraId] = [];
        return presetCache[cameraId];
      })
      .finally(function () { delete presetLoading[cameraId]; });
  }
  return presetLoading[cameraId];
}

function presetOptions(cameraId, selectedToken, selectedName) {
  const opts = [{ value: '', label: 'Current position' }];
  (presetCache[cameraId] || []).forEach(function (p) {
    opts.push({ value: p.token, label: p.name || p.token });
  });
  if (selectedToken && !opts.some(function (option) { return option.value === selectedToken; })) {
    opts.push({ value: selectedToken, label: selectedName || selectedToken });
  }
  return opts;
}

function setSelectOptions(select, options, value) {
  select.innerHTML = '';
  options.forEach(function (entry) {
    const option = document.createElement('option');
    option.value = String(entry.value);
    option.textContent = entry.label;
    select.appendChild(option);
  });
  select.value = value == null ? '' : String(value);
}

function detectorName(zoneId) {
  const detector = detectorOptions().find(function (entry) { return Number(entry.id) === Number(zoneId); });
  return detector ? detector.name : 'Unknown detector';
}

function cameraName(cameraId) {
  const camera = (state.cameras || []).find(function (entry) { return entry.id === cameraId; });
  return camera ? (camera.display_name || camera.id) : (cameraId || 'No camera');
}

function renderPresetSelect() {
  if (!stagedPairing) return;
  setSelectOptions(
    els.scenePairingPreset,
    presetOptions(stagedPairing.camera_id, stagedPairing.preset_token, stagedPairing.preset_name),
    stagedPairing.preset_token
  );
  els.scenePairingPreset.disabled = !stagedPairing.camera_id;
}

async function openPairingEditor(index, trigger) {
  editorIndex = index;
  const source = index == null ? pairingDefaults() : state.scenePairings[index];
  stagedPairing = { ...source };
  editorPairingId = stagedPairing.id;
  editorReturnFocus = trigger || null;
  els.scenePairingEditorTitle.textContent = index == null ? 'Add pairing' : 'Edit pairing';
  setToggleState(els.scenePairingEnabled, stagedPairing.enabled);
  setSelectOptions(
    els.scenePairingZone,
    [{ value: '', label: 'Select…' }].concat(detectorOptions().map(function (entry) {
      return { value: entry.id, label: entry.name };
    })),
    stagedPairing.zone_id
  );
  setSelectOptions(
    els.scenePairingCamera,
    [{ value: '', label: 'Select…' }].concat((state.cameras || []).map(function (entry) {
      return { value: entry.id, label: entry.display_name || entry.id };
    })),
    stagedPairing.camera_id
  );
  renderPresetSelect();
  els.scenePairingDelete.hidden = index == null;
  if (typeof els.scenePairingDialog.showModal === 'function') els.scenePairingDialog.showModal();
  else els.scenePairingDialog.setAttribute('open', '');
  els.scenePairingZone.focus();
  const cameraId = stagedPairing.camera_id;
  await fetchPresets(cameraId);
  if (stagedPairing && stagedPairing.camera_id === cameraId) renderPresetSelect();
}

function closePairingEditor() {
  if (typeof els.scenePairingDialog.close === 'function') els.scenePairingDialog.close();
  else els.scenePairingDialog.removeAttribute('open');
}

function restoreEditorFocus() {
  let target = editorReturnFocus && editorReturnFocus.isConnected ? editorReturnFocus : null;
  if (!target && editorPairingId) {
    const row = els.scenePairings.querySelector('[data-pairing-id="' + CSS.escape(editorPairingId) + '"]');
    if (row) target = row.querySelector('.automation-summary-main');
  }
  if (!target) target = els.scenePairingAdd;
  editorIndex = null;
  editorPairingId = null;
  editorReturnFocus = null;
  stagedPairing = null;
  if (target) requestAnimationFrame(function () { target.focus(); });
}

export function renderScenePairings() {
  if (!els.scenePairings || !els.scenePairingsNote) return;
  els.scenePairings.innerHTML = '';
  state.scenePairings = normalizedPairings();

  const cameras = (state.cameras || []).map(function (c) {
    return { value: c.id, label: (c.display_name || c.id) };
  });

  if (els.scenePairingsCount) {
    const enabled = state.scenePairings.filter(function (p) { return p.enabled !== false; }).length;
    els.scenePairingsCount.hidden = enabled === 0;
    els.scenePairingsCount.textContent = enabled + ' active';
  }

  if (!state.scenePairings.length) {
    els.scenePairingsNote.hidden = false;
    els.scenePairingsNote.textContent = cameras.length
      ? 'No detector→camera pairings. Add one so a tripped detector captures its camera.'
      : 'No cameras configured — add cameras before pairing detectors.';
    return;
  }
  els.scenePairingsNote.hidden = true;

  state.scenePairings.forEach(function (entry, idx) {
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.pairingId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit scene pairing for ' + detectorName(entry.zone_id));
    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = detectorName(entry.zone_id);
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = cameraName(entry.camera_id) + ' · ' + (entry.preset_name || 'Current position');
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);
    main.insertAdjacentHTML('beforeend', icon('chevron-right', 'automation-summary-chevron'));
    main.addEventListener('click', function () { openPairingEditor(idx, main); });
    row.appendChild(main);

    const enabled = buildToggle('scene-pairing-enabled', entry.enabled, function (on) {
      const proposed = state.scenePairings.map(function (pairing, pairingIndex) {
        return pairingIndex === idx ? { ...pairing, enabled: on } : pairing;
      });
      saveScenePairings(proposed);
    });
    enabled.setAttribute('aria-label', 'Enable scene pairing for ' + detectorName(entry.zone_id));
    row.appendChild(enabled);
    els.scenePairings.appendChild(row);
  });
}

export async function loadScenePairings() {
  if (!els.scenePairings) return;
  try {
    const results = await Promise.all([
      jsonApi('/api/security/scene-pairings'),
      jsonApi('/api/cameras'),
    ]);
    state.scenePairings = (results[0] && results[0].entries) || [];
    state.cameras = (results[1] && results[1].cameras) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.scenePairings = state.scenePairings || [];
    if (els.scenePairingsNote) {
      els.scenePairingsNote.hidden = false;
      els.scenePairingsNote.textContent = exc.message || 'Failed to load pairings.';
    }
  }
  renderScenePairings();
}

async function saveScenePairings(entries) {
  const previous = state.scenePairings;
  state.scenePairings = normalizedPairings(entries);
  renderScenePairings();
  const complete = state.scenePairings.filter(function (p) {
    return p.zone_id != null && p.camera_id;
  });
  try {
    const body = await jsonApi('/api/security/scene-pairings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: complete }),
    });
    state.scenePairings = (body && body.entries) || [];
    renderScenePairings();
    toast('Pairings saved', 'success');
    return true;
  } catch (exc) {
    state.scenePairings = previous;
    renderScenePairings();
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save pairing", 'error');
    }
    return false;
  }
}

export function wireScenePairings() {
  if (!els.scenePairingAdd || !els.scenePairingDialog) return;
  wireToggle(els.scenePairingEnabled, function (on) {
    if (stagedPairing) stagedPairing.enabled = on;
  });
  els.scenePairingAdd.addEventListener('click', function () {
    openPairingEditor(null, els.scenePairingAdd);
  });
  els.scenePairingEditorClose.addEventListener('click', closePairingEditor);
  els.scenePairingDialog.addEventListener('click', function (ev) {
    if (ev.target === els.scenePairingDialog) closePairingEditor();
  });
  els.scenePairingDialog.addEventListener('close', restoreEditorFocus);
  els.scenePairingZone.addEventListener('change', function () {
    if (!stagedPairing) return;
    stagedPairing.zone_id = els.scenePairingZone.value === '' ? null : Number(els.scenePairingZone.value);
  });
  els.scenePairingCamera.addEventListener('change', async function () {
    if (!stagedPairing) return;
    stagedPairing.camera_id = els.scenePairingCamera.value;
    stagedPairing.preset_token = null;
    stagedPairing.preset_name = null;
    renderPresetSelect();
    const cameraId = stagedPairing.camera_id;
    await fetchPresets(cameraId);
    if (stagedPairing && stagedPairing.camera_id === cameraId) renderPresetSelect();
  });
  els.scenePairingPreset.addEventListener('change', function () {
    if (!stagedPairing) return;
    stagedPairing.preset_token = els.scenePairingPreset.value || null;
    stagedPairing.preset_name = stagedPairing.preset_token
      ? (els.scenePairingPreset.options[els.scenePairingPreset.selectedIndex].textContent || null)
      : null;
  });
  els.scenePairingSave.addEventListener('click', async function () {
    if (!stagedPairing) return;
    if (stagedPairing.zone_id == null) {
      toast('Choose a detector', 'warning');
      els.scenePairingZone.focus();
      return;
    }
    if (!stagedPairing.camera_id) {
      toast('Choose a camera', 'warning');
      els.scenePairingCamera.focus();
      return;
    }
    stagedPairing.enabled = isToggleOn(els.scenePairingEnabled);
    const proposed = state.scenePairings.slice();
    if (editorIndex == null) proposed.push(stagedPairing);
    else proposed[editorIndex] = stagedPairing;
    els.scenePairingSave.disabled = true;
    const saved = await saveScenePairings(proposed);
    els.scenePairingSave.disabled = false;
    if (saved) closePairingEditor();
  });
  els.scenePairingDelete.addEventListener('click', async function () {
    if (editorIndex == null) return;
    const ok = await confirmAction({
      title: 'Delete this scene pairing?',
      message: 'This detector-to-camera pairing will be removed permanently.',
      okLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const proposed = state.scenePairings.filter(function (_entry, idx) { return idx !== editorIndex; });
    if (await saveScenePairings(proposed)) closePairingEditor();
  });
}
