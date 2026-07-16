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
import { denseListEditor } from './dense-editor.js';

// cameraId -> [{token, name}], fetched lazily so we don't hit every camera up front.
const presetCache = {};
const presetLoading = {};

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
  const staged = pairingEditor.staged;
  if (!staged) return;
  setSelectOptions(
    els.scenePairingPreset,
    presetOptions(staged.camera_id, staged.preset_token, staged.preset_name),
    staged.preset_token
  );
  els.scenePairingPreset.disabled = !staged.camera_id;
}

const pairingEditor = denseListEditor({
  dialog: els.scenePairingDialog,
  addButton: els.scenePairingAdd,
  closeButton: els.scenePairingEditorClose,
  saveButton: els.scenePairingSave,
  deleteButton: els.scenePairingDelete,
  titleEl: els.scenePairingEditorTitle,
  listEl: els.scenePairings,
  focusEl: els.scenePairingZone,
  rowIdAttr: 'data-pairing-id',
  titles: { add: 'Add pairing', edit: 'Edit pairing' },
  deleteConfirm: {
    title: 'Delete this scene pairing?',
    message: 'This detector-to-camera pairing will be removed permanently.',
  },
  toasts: { saved: 'Pairings saved', failed: "Couldn't save pairing" },
  defaults: pairingDefaults,
  getEntries: function () { return state.scenePairings; },
  setEntries: function (entries) { state.scenePairings = entries; },
  normalize: normalizedPairings,
  render: renderScenePairings,
  populate: function (staged) {
    setToggleState(els.scenePairingEnabled, staged.enabled);
    setSelectOptions(
      els.scenePairingZone,
      [{ value: '', label: 'Select…' }].concat(detectorOptions().map(function (entry) {
        return { value: entry.id, label: entry.name };
      })),
      staged.zone_id
    );
    setSelectOptions(
      els.scenePairingCamera,
      [{ value: '', label: 'Select…' }].concat((state.cameras || []).map(function (entry) {
        return { value: entry.id, label: entry.display_name || entry.id };
      })),
      staged.camera_id
    );
    renderPresetSelect();
  },
  afterOpen: async function (staged) {
    const cameraId = staged.camera_id;
    await fetchPresets(cameraId);
    if (pairingEditor.staged && pairingEditor.staged.camera_id === cameraId) renderPresetSelect();
  },
  collect: function (staged) {
    if (staged.zone_id == null) {
      toast('Choose a detector', 'warning');
      els.scenePairingZone.focus();
      return false;
    }
    if (!staged.camera_id) {
      toast('Choose a camera', 'warning');
      els.scenePairingCamera.focus();
      return false;
    }
    staged.enabled = isToggleOn(els.scenePairingEnabled);
  },
  endpoint: '/api/security/scene-pairings',
  bodyKey: 'entries',
  payloadEntries: function (entries) {
    return entries.filter(function (p) { return p.zone_id != null && p.camera_id; });
  },
});

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
    main.addEventListener('click', function () { pairingEditor.open(idx, main); });
    row.appendChild(main);

    const enabled = buildToggle('scene-pairing-enabled', entry.enabled, function (on) {
      const proposed = state.scenePairings.map(function (pairing, pairingIndex) {
        return pairingIndex === idx ? { ...pairing, enabled: on } : pairing;
      });
      pairingEditor.save(proposed);
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

export function wireScenePairings() {
  if (!els.scenePairingAdd || !els.scenePairingDialog) return;
  wireToggle(els.scenePairingEnabled, function (on) {
    if (pairingEditor.staged) pairingEditor.staged.enabled = on;
  });
  els.scenePairingZone.addEventListener('change', function () {
    const staged = pairingEditor.staged;
    if (!staged) return;
    staged.zone_id = els.scenePairingZone.value === '' ? null : Number(els.scenePairingZone.value);
  });
  els.scenePairingCamera.addEventListener('change', async function () {
    const staged = pairingEditor.staged;
    if (!staged) return;
    staged.camera_id = els.scenePairingCamera.value;
    staged.preset_token = null;
    staged.preset_name = null;
    renderPresetSelect();
    const cameraId = staged.camera_id;
    await fetchPresets(cameraId);
    if (pairingEditor.staged && pairingEditor.staged.camera_id === cameraId) renderPresetSelect();
  });
  els.scenePairingPreset.addEventListener('change', function () {
    const staged = pairingEditor.staged;
    if (!staged) return;
    staged.preset_token = els.scenePairingPreset.value || null;
    staged.preset_name = staged.preset_token
      ? (els.scenePairingPreset.options[els.scenePairingPreset.selectedIndex].textContent || null)
      : null;
  });
  pairingEditor.wire();
}
