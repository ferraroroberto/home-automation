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
import { icon } from './_vendored/icons/icons.js';
import { detectorOptions } from './security-shared.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { confirmAction } from './network.js';

const RETRY_OPTIONS = [1, 2, 3];
let editorIndex = null;
let editorOverrideId = null;
let editorReturnFocus = null;
let stagedOverride = null;

function overrideDefaults() {
  return {
    id: 'override-' + Date.now().toString(36),
    enabled: true,
    zone_id: null,
    max_retries: 1,
  };
}

function normalizedOverrides(entries) {
  return (entries || state.securityOverrides || []).map(function (entry, idx) {
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

function openOverrideEditor(index, trigger) {
  editorIndex = index;
  const source = index == null ? overrideDefaults() : state.securityOverrides[index];
  stagedOverride = { ...source };
  editorOverrideId = stagedOverride.id;
  editorReturnFocus = trigger || null;
  els.securityOverrideEditorTitle.textContent = index == null ? 'Add override' : 'Edit override';
  setToggleState(els.securityOverrideEnabled, stagedOverride.enabled);
  setSelectOptions(
    els.securityOverrideZone,
    [{ value: '', label: 'Select…' }].concat(detectorOptions().map(function (entry) {
      return { value: entry.id, label: entry.name };
    })),
    stagedOverride.zone_id
  );
  setSelectOptions(
    els.securityOverrideRetries,
    RETRY_OPTIONS.map(function (count) {
      return { value: count, label: count + (count === 1 ? ' trigger' : ' triggers') };
    }),
    stagedOverride.max_retries
  );
  els.securityOverrideDelete.hidden = index == null;
  if (typeof els.securityOverrideDialog.showModal === 'function') els.securityOverrideDialog.showModal();
  else els.securityOverrideDialog.setAttribute('open', '');
  els.securityOverrideZone.focus();
}

function closeOverrideEditor() {
  if (typeof els.securityOverrideDialog.close === 'function') els.securityOverrideDialog.close();
  else els.securityOverrideDialog.removeAttribute('open');
}

function restoreEditorFocus() {
  let target = editorReturnFocus && editorReturnFocus.isConnected ? editorReturnFocus : null;
  if (!target && editorOverrideId) {
    const row = els.securityOverrides.querySelector('[data-override-id="' + CSS.escape(editorOverrideId) + '"]');
    if (row) target = row.querySelector('.automation-summary-main');
  }
  if (!target) target = els.securityOverrideAdd;
  editorIndex = null;
  editorOverrideId = null;
  editorReturnFocus = null;
  stagedOverride = null;
  if (target) requestAnimationFrame(function () { target.focus(); });
}

export function renderSecurityOverrides() {
  if (!els.securityOverrides || !els.securityOverridesNote) return;
  els.securityOverrides.innerHTML = '';
  state.securityOverrides = normalizedOverrides();

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
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.overrideId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit override for ' + detectorName(entry.zone_id));
    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = detectorName(entry.zone_id);
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = 'Bypass after ' + entry.max_retries +
      (entry.max_retries === 1 ? ' trigger' : ' triggers');
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);
    main.insertAdjacentHTML('beforeend', icon('chevron-right', 'automation-summary-chevron'));
    main.addEventListener('click', function () { openOverrideEditor(idx, main); });
    row.appendChild(main);

    const enabled = buildToggle('security-override-enabled', entry.enabled, function (on) {
      const proposed = state.securityOverrides.map(function (override, overrideIndex) {
        return overrideIndex === idx ? { ...override, enabled: on } : override;
      });
      saveSecurityOverrides(proposed);
    });
    enabled.setAttribute('aria-label', 'Enable override for ' + detectorName(entry.zone_id));
    row.appendChild(enabled);
    els.securityOverrides.appendChild(row);
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

async function saveSecurityOverrides(entries) {
  const previous = state.securityOverrides;
  state.securityOverrides = normalizedOverrides(entries);
  renderSecurityOverrides();
  const complete = state.securityOverrides.filter(function (entry) { return entry.zone_id != null; });
  try {
    const body = await jsonApi('/api/security/overrides', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: complete }),
    });
    state.securityOverrides = (body && body.entries) || [];
    renderSecurityOverrides();
    toast('Override saved', 'success');
    return true;
  } catch (exc) {
    state.securityOverrides = previous;
    renderSecurityOverrides();
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save override", 'error');
    }
    return false;
  }
}

export function wireSecurityOverrides() {
  if (!els.securityOverrideAdd || !els.securityOverrideDialog) return;
  wireToggle(els.securityOverrideEnabled, function (on) {
    if (stagedOverride) stagedOverride.enabled = on;
  });
  els.securityOverrideAdd.addEventListener('click', function () {
    openOverrideEditor(null, els.securityOverrideAdd);
  });
  els.securityOverrideEditorClose.addEventListener('click', closeOverrideEditor);
  els.securityOverrideDialog.addEventListener('click', function (ev) {
    if (ev.target === els.securityOverrideDialog) closeOverrideEditor();
  });
  els.securityOverrideDialog.addEventListener('close', restoreEditorFocus);
  els.securityOverrideZone.addEventListener('change', function () {
    if (!stagedOverride) return;
    stagedOverride.zone_id = els.securityOverrideZone.value === ''
      ? null : Number(els.securityOverrideZone.value);
  });
  els.securityOverrideRetries.addEventListener('change', function () {
    if (stagedOverride) stagedOverride.max_retries = Number(els.securityOverrideRetries.value);
  });
  els.securityOverrideSave.addEventListener('click', async function () {
    if (!stagedOverride) return;
    if (stagedOverride.zone_id == null) {
      toast('Choose a detector', 'warning');
      els.securityOverrideZone.focus();
      return;
    }
    stagedOverride.enabled = isToggleOn(els.securityOverrideEnabled);
    const proposed = state.securityOverrides.slice();
    if (editorIndex == null) proposed.push(stagedOverride);
    else proposed[editorIndex] = stagedOverride;
    els.securityOverrideSave.disabled = true;
    const saved = await saveSecurityOverrides(proposed);
    els.securityOverrideSave.disabled = false;
    if (saved) closeOverrideEditor();
  });
  els.securityOverrideDelete.addEventListener('click', async function () {
    if (editorIndex == null) return;
    const ok = await confirmAction({
      title: 'Delete this alarm override?',
      message: 'This detector override will be removed permanently.',
      okLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const proposed = state.securityOverrides.filter(function (_entry, idx) { return idx !== editorIndex; });
    if (await saveSecurityOverrides(proposed)) closeOverrideEditor();
  });
}
