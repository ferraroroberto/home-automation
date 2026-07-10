/* Elgato Lights tab controller.
 *
 * Reads GET /api/lights and writes POST /api/lights/{id}. Polling is tab-aware
 * like Plugs: the LAN read runs only while the Lights tab is open. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { emptyStateEl } from './icons.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';

const POLL_MS = 15_000;

function label(light) {
  return light.display_name || light.name || light.light_id || 'Elgato light';
}

function lightById(lightId) {
  return state.lights.find(function (light) { return light.light_id === lightId; });
}

function originalName(light) {
  return light.name || light.product_name || light.light_id || 'Elgato light';
}

function fmtTemperature(light) {
  if (light.temperature_k) return light.temperature_k + ' K';
  if (light.temperature) return light.temperature + ' mired';
  return '—';
}

function fmtTemperatureDetail(light) {
  if (!light.supports_temperature) return 'Brightness only';
  if (light.temperature && light.temperature_k) {
    return light.temperature + ' mired · ' + light.temperature_k + ' K';
  }
  return fmtTemperature(light);
}

function reachableLights() {
  return state.lights.filter(function (light) { return light.reachable; });
}

function bulkTargets(on) {
  return reachableLights().filter(function (light) { return light.on !== on; });
}

function updateBulkControls() {
  if (!els.lightsAllOn || !els.lightsAllOff) return;
  const reachable = reachableLights();
  const allOn = reachable.length > 0 && reachable.every(function (light) { return light.on === true; });
  const allOff = reachable.length > 0 && reachable.every(function (light) { return light.on !== true; });
  els.lightsAllOn.disabled = !reachable.length || allOn;
  els.lightsAllOff.disabled = !reachable.length || allOff;
}

function wait(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

async function applyLight(light, patch) {
  // Toast only the on/off command — brightness/temperature sliders call this
  // rapidly and would otherwise spam the toast (#204).
  const isToggle = Object.prototype.hasOwnProperty.call(patch, 'on');
  try {
    if (isToggle) toast('Sending…', 'pending');
    const updated = await jsonApi('/api/lights/' + encodeURIComponent(light.light_id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    state.lights = state.lights.map(function (item) {
      return item.light_id === updated.light_id ? Object.assign({}, item, updated) : item;
    });
    renderLights();
    if (isToggle) toast(label(updated) + (patch.on ? ' on' : ' off'), 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function applyAllLights(on) {
  const targets = bulkTargets(on);
  if (!reachableLights().length) {
    toast('No reachable lights', 'error');
    return;
  }
  if (!targets.length) return;
  toast((on ? 'Activating ' : 'Deactivating ') + targets.length + ' light' + (targets.length === 1 ? '' : 's'));
  await wait(250);
  let failures = 0;
  for (const light of targets) {
    try {
      const updated = await jsonApi('/api/lights/' + encodeURIComponent(light.light_id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: on }),
      });
      state.lights = state.lights.map(function (item) {
        return item.light_id === updated.light_id ? Object.assign({}, item, updated) : item;
      });
      renderLights();
      toast(label(updated) + (on ? ' on' : ' off'));
      await wait(250);
    } catch (exc) {
      failures += 1;
      if (String(exc.message) !== 'auth required') {
        toast('Failed: ' + label(light) + ': ' + (exc.message || exc), 'error');
        await wait(250);
      }
    }
  }
  await loadLights();
  if (failures) toast(failures + ' light command(s) failed', 'error');
}

function buildSlider(light, key, min, max, value, suffix) {
  const row = document.createElement('div');
  row.className = 'light-control-row';
  const labelEl = document.createElement('span');
  labelEl.className = 'light-control-label';
  labelEl.textContent = key;
  const controls = document.createElement('div');
  controls.className = 'light-range-control';
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = String(min);
  slider.max = String(max);
  slider.value = String(value);
  slider.className = 'light-slider';
  slider.setAttribute('aria-label', key + ' for ' + label(light));
  const number = document.createElement('input');
  number.type = 'number';
  number.min = String(min);
  number.max = String(max);
  number.step = '1';
  number.value = String(value);
  number.className = 'input-native light-number';
  number.setAttribute('aria-label', key + ' exact value for ' + label(light));
  const valueEdit = document.createElement('label');
  valueEdit.className = 'light-value-edit';
  const unit = document.createElement('span');
  unit.className = 'light-value-unit';
  unit.textContent = suffix === 'K' ? 'K' : suffix;
  const paintSlider = function (next) {
    const pct = ((Number(next) - min) / (max - min)) * 100;
    slider.style.setProperty('--light-slider-pct', Math.max(0, Math.min(100, pct)) + '%');
  };
  const sync = function (next) {
    slider.value = String(next);
    number.value = String(next);
    paintSlider(next);
  };
  const commit = function (raw) {
    let next = Math.round(Number(raw));
    if (!Number.isFinite(next)) next = value;
    next = Math.max(min, Math.min(max, next));
    sync(next);
    const field = suffix === 'K' ? 'temperature_k' : 'brightness';
    const patch = {};
    patch[field] = next;
    applyLight(light, patch);
  };
  slider.addEventListener('input', function () { sync(slider.value); });
  slider.addEventListener('change', function () { commit(slider.value); });
  number.addEventListener('change', function () { commit(number.value); });
  number.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); number.blur(); }
  });
  sync(value);
  valueEdit.appendChild(number);
  valueEdit.appendChild(unit);
  row.appendChild(labelEl);
  controls.appendChild(slider);
  controls.appendChild(valueEdit);
  row.appendChild(controls);
  return row;
}

function buildCard(light) {
  const on = light.on === true;
  const card = document.createElement('article');
  card.className = 'card light-card';
  card.dataset.lightId = light.light_id;
  if (!light.reachable) card.classList.add('is-unavailable');
  else if (!on) card.classList.add('is-off');

  const top = document.createElement('div');
  top.className = 'light-top';

  const text = document.createElement('div');
  text.className = 'light-title';
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'light-name';
  name.title = 'Rename';
  name.textContent = label(light);
  name.addEventListener('click', function () { openLightDetail(light.light_id); });
  text.appendChild(name);
  const meta = document.createElement('span');
  meta.className = 'light-meta';
  meta.textContent = light.product_name || originalName(light);
  text.appendChild(meta);
  top.appendChild(text);

  if (light.reachable) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle' + (on ? ' on' : '');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Power ' + label(light));
    toggle.innerHTML = toggleMarkup(on);
    toggle.addEventListener('click', function () { applyLight(light, { on: !on }); });
    top.appendChild(toggle);
  }
  card.appendChild(top);

  if (!light.reachable) {
    const note = document.createElement('div');
    note.className = 'light-unavailable';
    note.textContent = light.error || 'Unavailable';
    card.appendChild(note);
    return card;
  }

  const controls = document.createElement('div');
  controls.className = 'light-controls';
  controls.appendChild(
    buildSlider(light, 'Brightness', 3, 100, Number(light.brightness || 3), '%')
  );
  if (light.supports_temperature) {
    controls.appendChild(
      buildSlider(light, 'Warmth', 2900, 7000, Number(light.temperature_k || 2900), 'K')
    );
  } else {
    const unavailable = document.createElement('div');
    unavailable.className = 'light-unavailable';
    unavailable.textContent = 'Color temperature unavailable';
    controls.appendChild(unavailable);
  }
  card.appendChild(controls);

  return card;
}

function openLightDetail(lightId) {
  const light = lightById(lightId);
  if (!light) return;
  state.selectedLightId = lightId;
  els.lightDetailName.textContent = label(light);
  els.lightDisplayName.value = light.display_name || '';
  els.lightDisplayName.placeholder = originalName(light);
  els.lightOriginalName.textContent = originalName(light);
  els.lightProduct.textContent = light.product_name || '—';
  els.lightHost.textContent = light.host || '—';
  els.lightPort.textContent = light.port == null ? '—' : String(light.port);
  els.lightMac.textContent = light.mac_address || 'Unavailable';
  els.lightFirmware.textContent = light.firmware || '—';
  els.lightIdentifier.textContent = light.light_id || '—';
  els.lightTemperatureMeta.textContent = fmtTemperatureDetail(light);
  if (els.lightSave) els.lightSave.disabled = true;
  if (typeof els.lightDialog.showModal === 'function') els.lightDialog.showModal();
  else els.lightDialog.setAttribute('open', '');
  els.lightDisplayName.focus();
}

function closeLightDetail() {
  state.selectedLightId = null;
  if (typeof els.lightDialog.close === 'function') els.lightDialog.close();
  else els.lightDialog.removeAttribute('open');
}

async function saveLightName() {
  if (!state.selectedLightId) return;
  const lightId = state.selectedLightId;
  const displayKey = (lightById(lightId) || {}).display_key || lightId;
  const displayName = els.lightDisplayName.value.trim();
  try {
    await jsonApi('/api/lights/' + encodeURIComponent(lightId) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName, display_key: displayKey }),
    });
    state.lights = state.lights.map(function (light) {
      return light.light_id === lightId ? Object.assign({}, light, { display_name: displayName || null }) : light;
    });
    const updatedLight = lightById(lightId);
    if (updatedLight) els.lightDetailName.textContent = label(updatedLight);
    renderLights();
    if (els.lightSave) els.lightSave.disabled = true;
    toast('Saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

// Canonical empty-state block (issue #362): icon + one-line reason + Retry,
// rendered into the grid itself whenever there are zero lights — whether that
// is a clean "none configured" response or the aftermath of a failed fetch.
// `lightsNote` still carries the longer diagnostic/setup hint underneath.
function showLightsEmpty() {
  els.lightsGrid.innerHTML = '';
  els.lightsGrid.appendChild(emptyStateEl('lightbulb', 'No lights reachable', {
    actionLabel: 'Retry',
    onAction: function () { loadLights(); },
  }));
}

export function renderLights() {
  els.lightsGrid.innerHTML = '';
  if (!state.lights.length) {
    updateBulkControls();
    showLightsEmpty();
    els.lightsNote.hidden = false;
    els.lightsNote.textContent =
      'Add ELGATO_LIGHT_HOSTS=host[:9123] to .env or enable Bonjour/mDNS.';
    return;
  }
  if (isSnapshotRestored('lights')) {
    els.lightsNote.hidden = false;
    els.lightsNote.textContent = snapshotLabel('lights');
  } else {
    els.lightsNote.hidden = true;
  }
  const sorted = state.lights.slice().sort(function (a, b) {
    return label(a).localeCompare(label(b));
  });
  sorted.forEach(function (light) { els.lightsGrid.appendChild(buildCard(light)); });
  updateBulkControls();
}

export async function loadLights() {
  try {
    const body = await jsonApi('/api/lights');
    reportFetchOk('lights');
    saveSnapshot('lights', body);
    state.lights = (body && body.lights) || [];
    renderLights();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('lights', exc, 'lights');
    if (!state.lights.length) showLightsEmpty();
    updateBulkControls();
    els.lightsNote.hidden = false;
    els.lightsNote.textContent = state.lights.length
      ? 'Showing last successful light data. Refresh failed: ' + (exc.message || 'Failed to load Elgato lights.')
      : (exc.message || 'Failed to load Elgato lights.');
  }
}

export function restoreLightsSnapshot() {
  const body = restoreSnapshot('lights');
  if (!body) return;
  state.lights = (body && body.lights) || [];
  renderLights();
}

const schedule = createPoller(loadLights);

export function onLightsTab(tab) {
  if (tab === 'lights') {
    loadLights();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}

export function wireLightControls() {
  if (els.lightsRefresh) {
    els.lightsRefresh.addEventListener('click', async function () {
      els.lightsRefresh.disabled = true;
      try {
        const body = await jsonApi('/api/lights/refresh', { method: 'POST' });
        reportFetchOk('lights');
        saveSnapshot('lights', body);
        state.lights = (body && body.lights) || [];
        renderLights();
        toast('Lights refreshed', 'success');
      } catch (exc) {
        if (String(exc.message) !== 'auth required') {
          reportFetchFailure('lights', exc, 'lights');
          els.lightsNote.hidden = false;
          els.lightsNote.textContent = state.lights.length
            ? 'Showing last successful light data. Refresh failed: ' + (exc.message || 'Failed to load Elgato lights.')
            : (exc.message || 'Failed to load Elgato lights.');
        }
      } finally {
        els.lightsRefresh.disabled = false;
      }
    });
  }
  if (els.lightsAllOn) {
    els.lightsAllOn.addEventListener('click', function () { applyAllLights(true); });
  }
  if (els.lightsAllOff) {
    els.lightsAllOff.addEventListener('click', function () { applyAllLights(false); });
  }
  els.lightDetailClose.addEventListener('click', closeLightDetail);
  els.lightDialog.addEventListener('click', function (ev) {
    if (ev.target === els.lightDialog) closeLightDetail();
  });
  els.lightDisplayName.addEventListener('input', function () {
    if (els.lightSave) els.lightSave.disabled = false;
  });
  if (els.lightSave) els.lightSave.addEventListener('click', saveLightName);
  els.lightDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveLightName(); }
  });
}
