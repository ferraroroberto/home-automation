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
import { detectorOptions, buildSelect } from './security-shared.js';
import { buildToggle } from './toggle.js';

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

function normalizedPairings() {
  return (state.scenePairings || []).map(function (entry, idx) {
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
  if (!cameraId || presetCache[cameraId] || presetLoading[cameraId]) return;
  presetLoading[cameraId] = true;
  try {
    const body = await jsonApi('/api/cameras/' + encodeURIComponent(cameraId) + '/presets');
    presetCache[cameraId] = (body && body.presets) || [];
    renderScenePairings();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') presetCache[cameraId] = [];
  } finally {
    delete presetLoading[cameraId];
  }
}

function presetOptions(cameraId) {
  const opts = [{ value: '', label: 'Current position' }];
  (presetCache[cameraId] || []).forEach(function (p) {
    opts.push({ value: p.token, label: p.name || p.token });
  });
  return opts;
}

export function renderScenePairings() {
  if (!els.scenePairings || !els.scenePairingsNote) return;
  els.scenePairings.innerHTML = '';
  state.scenePairings = normalizedPairings();

  const detectors = detectorOptions();
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
    fetchPresets(entry.camera_id);

    const card = document.createElement('div');
    card.className = 'schedule-entry scene-pairing-entry';
    card.dataset.pairingId = entry.id;

    const head = document.createElement('div');
    head.className = 'schedule-entry-head';

    const enabled = document.createElement('label');
    enabled.className = 'schedule-enabled';
    const enabledText = document.createElement('span');
    enabledText.textContent = 'Enabled';
    enabled.appendChild(enabledText);
    enabled.appendChild(buildToggle('scene-pairing-enabled', entry.enabled, function (on) {
      state.scenePairings[idx].enabled = on;
      saveScenePairings();
    }));
    head.appendChild(enabled);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'schedule-delete';
    del.setAttribute('aria-label', 'Delete pairing');
    del.textContent = '×';
    del.addEventListener('click', function () {
      state.scenePairings.splice(idx, 1);
      saveScenePairings();
    });
    head.appendChild(del);
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'scene-pairing-fields';

    // Detector
    const zoneLabel = document.createElement('label');
    const zoneText = document.createElement('span');
    zoneText.textContent = 'Detector';
    const zoneOpts = [{ value: '', label: 'Select…' }].concat(
      detectors.map(function (d) { return { value: d.id, label: d.name }; })
    );
    const zoneSel = buildSelect('scene-pairing-zone', zoneOpts, entry.zone_id, function (ev) {
      const v = ev.target.value;
      state.scenePairings[idx].zone_id = v === '' ? null : Number(v);
      saveScenePairings();
    });
    zoneLabel.appendChild(zoneText);
    zoneLabel.appendChild(zoneSel);
    fields.appendChild(zoneLabel);

    // Camera
    const camLabel = document.createElement('label');
    const camText = document.createElement('span');
    camText.textContent = 'Camera';
    const camOpts = [{ value: '', label: 'Select…' }].concat(cameras);
    const camSel = buildSelect('scene-pairing-camera', camOpts, entry.camera_id, function (ev) {
      state.scenePairings[idx].camera_id = ev.target.value;
      // Reset the preset when the camera changes — tokens are camera-specific.
      state.scenePairings[idx].preset_token = null;
      state.scenePairings[idx].preset_name = null;
      fetchPresets(ev.target.value);
      saveScenePairings();
    });
    camLabel.appendChild(camText);
    camLabel.appendChild(camSel);
    fields.appendChild(camLabel);

    // Preset (PTZ position)
    const presetLabel = document.createElement('label');
    const presetText = document.createElement('span');
    presetText.textContent = 'Position';
    const presetSel = buildSelect(
      'scene-pairing-preset', presetOptions(entry.camera_id), entry.preset_token, function (ev) {
        const token = ev.target.value;
        state.scenePairings[idx].preset_token = token || null;
        state.scenePairings[idx].preset_name = token
          ? (ev.target.options[ev.target.selectedIndex].textContent || null)
          : null;
        saveScenePairings();
      }
    );
    presetLabel.appendChild(presetText);
    presetLabel.appendChild(presetSel);
    fields.appendChild(presetLabel);

    card.appendChild(fields);
    els.scenePairings.appendChild(card);
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

async function saveScenePairings() {
  state.scenePairings = normalizedPairings();
  renderScenePairings();
  // Only persist complete pairings — an in-progress row with no detector/camera
  // yet is kept in the UI but not sent (the backend would drop it anyway).
  const entries = state.scenePairings.filter(function (p) {
    return p.zone_id != null && p.camera_id;
  });
  try {
    const body = await jsonApi('/api/security/scene-pairings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: entries }),
    });
    // Re-merge any incomplete (unsaved) rows on top of the persisted set so the
    // user doesn't lose a half-filled row they're still editing.
    const saved = (body && body.entries) || [];
    const incomplete = state.scenePairings.filter(function (p) {
      return p.zone_id == null || !p.camera_id;
    });
    state.scenePairings = saved.concat(incomplete);
    renderScenePairings();
    toast('Pairings saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Pairing save failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireScenePairings() {
  if (!els.scenePairingAdd) return;
  els.scenePairingAdd.addEventListener('click', function () {
    state.scenePairings = normalizedPairings();
    state.scenePairings.push(pairingDefaults());
    renderScenePairings();
  });
}
