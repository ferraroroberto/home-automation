/* Alarm-override editor (issue #341).
 *
 * Per-detector "bypass this zone after N repeated alarms this armed session"
 * rules. RISCO's panel already does an uncontrolled version of this (issue
 * #325); this lets the user set a tighter, per-zone retry count (1-3) so a
 * windy garden or a roaming animal stops spamming the scene-capture pipeline
 * well before the panel's own limit. Mirrors the pairings editor's
 * load/normalise/render/save shape (security-scene.js), persisting through
 * GET/PUT /api/security/overrides.
 *
 * Detector options come from the already-loaded security state
 * (state.security.zones), same as the scene-pairings editor.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';

const RETRY_OPTIONS = [1, 2, 3];

function overrideDefaults() {
  return {
    id: 'override-' + Date.now().toString(36),
    enabled: true,
    zone_id: null,
    max_retries: 1,
  };
}

function normalizedOverrides() {
  return (state.securityOverrides || []).map(function (entry, idx) {
    const zoneId = Number(entry.zone_id);
    const retries = Number(entry.max_retries);
    return {
      id: entry.id || ('override-' + (idx + 1)),
      enabled: entry.enabled !== false,
      zone_id: Number.isFinite(zoneId) ? zoneId : null,
      max_retries: RETRY_OPTIONS.includes(retries) ? retries : 1,
    };
  });
}

function detectorOptions() {
  const zones = (state.security && state.security.zones) || [];
  return zones.map(function (zone) {
    return { id: zone.id, name: (zone.display_name || zone.name || String(zone.id)) };
  });
}

function buildSelect(className, options, value, onChange) {
  const sel = document.createElement('select');
  sel.className = 'select-native ' + className;
  options.forEach(function (opt) {
    const o = document.createElement('option');
    o.value = String(opt.value);
    o.textContent = opt.label;
    sel.appendChild(o);
  });
  sel.value = value == null ? '' : String(value);
  sel.addEventListener('change', onChange);
  return sel;
}

export function renderSecurityOverrides() {
  if (!els.securityOverrides || !els.securityOverridesNote) return;
  els.securityOverrides.innerHTML = '';
  state.securityOverrides = normalizedOverrides();

  const detectors = detectorOptions();

  if (els.securityOverridesCount) {
    const enabled = state.securityOverrides.filter(function (o) { return o.enabled !== false; }).length;
    els.securityOverridesCount.hidden = enabled === 0;
    els.securityOverridesCount.textContent = enabled + ' active';
  }

  if (!state.securityOverrides.length) {
    els.securityOverridesNote.hidden = false;
    els.securityOverridesNote.textContent =
      'No overrides configured. Add one so a repeatedly-tripped detector gets bypassed instead of spamming alerts.';
    return;
  }
  els.securityOverridesNote.hidden = true;

  state.securityOverrides.forEach(function (entry, idx) {
    const card = document.createElement('div');
    card.className = 'schedule-entry security-override-entry';
    card.dataset.overrideId = entry.id;

    const head = document.createElement('div');
    head.className = 'schedule-entry-head';

    const enabled = document.createElement('label');
    enabled.className = 'schedule-enabled';
    enabled.innerHTML = '<input type="checkbox" class="checkbox-native security-override-enabled"' +
      (entry.enabled ? ' checked' : '') + '> <span>Enabled</span>';
    enabled.querySelector('input').addEventListener('change', function (ev) {
      state.securityOverrides[idx].enabled = ev.target.checked;
      saveSecurityOverrides();
    });
    head.appendChild(enabled);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'schedule-delete';
    del.setAttribute('aria-label', 'Delete override');
    del.textContent = '×';
    del.addEventListener('click', function () {
      state.securityOverrides.splice(idx, 1);
      saveSecurityOverrides();
    });
    head.appendChild(del);
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'security-override-fields';

    // Detector
    const zoneLabel = document.createElement('label');
    const zoneText = document.createElement('span');
    zoneText.textContent = 'Detector';
    const zoneOpts = [{ value: '', label: 'Select…' }].concat(
      detectors.map(function (d) { return { value: d.id, label: d.name }; })
    );
    const zoneSel = buildSelect('security-override-zone', zoneOpts, entry.zone_id, function (ev) {
      const v = ev.target.value;
      state.securityOverrides[idx].zone_id = v === '' ? null : Number(v);
      saveSecurityOverrides();
    });
    zoneLabel.appendChild(zoneText);
    zoneLabel.appendChild(zoneSel);
    fields.appendChild(zoneLabel);

    // Retries
    const retriesLabel = document.createElement('label');
    const retriesText = document.createElement('span');
    retriesText.textContent = 'Bypass after';
    const retriesOpts = RETRY_OPTIONS.map(function (n) {
      return { value: n, label: n + (n === 1 ? ' trigger' : ' triggers') };
    });
    const retriesSel = buildSelect('security-override-retries', retriesOpts, entry.max_retries, function (ev) {
      state.securityOverrides[idx].max_retries = Number(ev.target.value);
      saveSecurityOverrides();
    });
    retriesLabel.appendChild(retriesText);
    retriesLabel.appendChild(retriesSel);
    fields.appendChild(retriesLabel);

    card.appendChild(fields);
    els.securityOverrides.appendChild(card);
  });
}

export async function loadSecurityOverrides() {
  if (!els.securityOverrides) return;
  try {
    const body = await jsonApi('/api/security/overrides');
    state.securityOverrides = (body && body.entries) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.securityOverrides = state.securityOverrides || [];
    if (els.securityOverridesNote) {
      els.securityOverridesNote.hidden = false;
      els.securityOverridesNote.textContent = exc.message || 'Failed to load overrides.';
    }
  }
  renderSecurityOverrides();
}

async function saveSecurityOverrides() {
  state.securityOverrides = normalizedOverrides();
  renderSecurityOverrides();
  // Only persist complete overrides — an in-progress row with no detector
  // chosen yet is kept in the UI but not sent (the backend would drop it anyway).
  const entries = state.securityOverrides.filter(function (o) { return o.zone_id != null; });
  try {
    const body = await jsonApi('/api/security/overrides', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: entries }),
    });
    // Re-merge any incomplete (unsaved) rows on top of the persisted set so the
    // user doesn't lose a half-filled row they're still editing.
    const saved = (body && body.entries) || [];
    const incomplete = state.securityOverrides.filter(function (o) { return o.zone_id == null; });
    state.securityOverrides = saved.concat(incomplete);
    renderSecurityOverrides();
    toast('Override saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Override save failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireSecurityOverrides() {
  if (!els.securityOverrideAdd) return;
  els.securityOverrideAdd.addEventListener('click', function () {
    state.securityOverrides = normalizedOverrides();
    state.securityOverrides.push(overrideDefaults());
    renderSecurityOverrides();
  });
}
