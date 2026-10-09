/* Lights controller (Devices tab).
 *
 * Reads GET /api/lights and writes POST /api/lights/{id}. Polling is tab-aware
 * like Plugs: the LAN read runs only while the Devices tab is open.
 *
 * The card also lists the Tuya lights (#181). Those are read by plugs.js as
 * part of GET /api/tuya and handed over as state.tuyaLights on every Plugs
 * render (the 'plugs:rendered' event); their rows and on/off reuse the plug
 * switch path, so there is one Tuya read and one Tuya write path. */

'use strict';

import { state, els, toast, reportFetchOk } from './state.js';
import { createViewState, markTabFailure } from './view-state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { emptyStateEl } from './empty-state.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';
import { closeDialog, openDialog } from './dialog.js';
import { buildPlugRow, setTuyaBrightness, toggleSwitch } from './plugs.js';
import { friendlyError } from './format.js';

const POLL_MS = 15_000;
const LIGHTS_UNAVAILABLE_COPY =
  'Live light data is unavailable. Check the light connection, then retry.';

const lightsView = createViewState('lights');

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

function reachableTuyaLights() {
  return state.tuyaLights.filter(function (device) { return device.reachable; });
}

function bulkTargets(on) {
  return reachableLights().filter(function (light) { return light.on !== on; });
}

function tuyaBulkTargets(on) {
  return reachableTuyaLights().filter(function (device) { return (device.switch_on === true) !== on; });
}

function updateBulkControls() {
  if (!els.lightsAllOn || !els.lightsAllOff) return;
  const onStates = reachableLights().map(function (light) { return light.on === true; })
    .concat(reachableTuyaLights().map(function (device) { return device.switch_on === true; }));
  const allOn = onStates.length > 0 && onStates.every(function (on) { return on; });
  const allOff = onStates.length > 0 && onStates.every(function (on) { return !on; });
  els.lightsAllOn.disabled = !onStates.length || allOn;
  els.lightsAllOff.disabled = !onStates.length || allOff;
}

function markLightsFailure() {
  markTabFailure(lightsView, {
    hasData: state.lights.length > 0,
    scope: 'lights',
    label: 'lights',
    render: renderLights,
  });
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
    reportActionFailure(exc, 'Failed');
  }
}

async function applyAllLights(on) {
  const targets = bulkTargets(on);
  const tuyaTargets = tuyaBulkTargets(on);
  if (!reachableLights().length && !reachableTuyaLights().length) {
    toast('No reachable lights', 'error');
    return;
  }
  const count = targets.length + tuyaTargets.length;
  if (!count) return;
  toast((on ? 'Activating ' : 'Deactivating ') + count + ' light' + (count === 1 ? '' : 's'));
  await wait(250);
  // Tuya lights go through the plug switch path, which toasts and reports
  // its own outcome and re-renders this card via 'plugs:rendered'.
  for (const device of tuyaTargets) {
    await toggleSwitch(device, null);
    await wait(250);
  }
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
      if (!isAuthRequired(exc)) {
        toast('Failed: ' + label(light) + ': ' + friendlyError(exc), 'error');
        await wait(250);
      }
    }
  }
  await loadLights();
  if (failures) toast(failures + ' light command(s) failed', 'error');
}

// One labelled slider + exact-number field. ``name`` labels it for assistive
// tech; ``apply(next)`` is called once per committed value (release / Enter),
// never per pixel — shared by the Elgato and Tuya light rows (#870).
function buildSlider(name, key, min, max, value, suffix, apply) {
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
  slider.setAttribute('aria-label', key + ' for ' + name);
  const number = document.createElement('input');
  number.type = 'number';
  number.min = String(min);
  number.max = String(max);
  number.step = '1';
  number.value = String(value);
  number.className = 'input-native light-number';
  number.setAttribute('aria-label', key + ' exact value for ' + name);
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
    apply(next);
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

// Lights render as compact divider-separated rows, the same shape as their
// Plugs/Blinds siblings on the IoT tab (#136) — a card per light nested inside
// the Lights card would read as double chrome, which design.md's list-row
// contract rejects. Row internals are unconstrained by that contract, so the
// name + toggle share the summary line and the sliders wrap onto their own line
// below. The product name is not repeated per row (it is in the detail modal);
// the row keeps the plug row's name-only identity.
function buildLightRow(light) {
  const on = light.on === true;
  const row = document.createElement('div');
  row.className = 'device-row light-row';
  row.dataset.lightId = light.light_id;

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'device-row-name';
  name.title = 'Rename';
  name.textContent = label(light);
  name.addEventListener('click', function () { openLightDetail(light.light_id); });
  row.appendChild(name);

  // Offline: name + reason only, no controls — the plug-row contract. The row
  // ellipsizes a long reason, so the full text also rides in the hover title.
  if (!light.reachable) {
    row.classList.add('is-unavailable');
    const note = document.createElement('span');
    note.className = 'device-row-note light-unavailable';
    // The row says only that the light is unavailable (#879): its connection
    // error carries the device address, which the UI never shows.
    note.textContent = 'Unavailable';
    row.appendChild(note);
    return row;
  }
  if (!on) row.classList.add('is-off');

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle' + (on ? ' on' : '');
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', on ? 'true' : 'false');
  toggle.setAttribute('aria-label', 'Power ' + label(light));
  toggle.innerHTML = toggleMarkup(on);
  toggle.addEventListener('click', function () { applyLight(light, { on: !on }); });
  row.appendChild(toggle);

  const controls = document.createElement('div');
  controls.className = 'light-controls';
  controls.appendChild(
    buildSlider(label(light), 'Brightness', 3, 100, Number(light.brightness || 3), '%',
      function (next) { applyLight(light, { brightness: next }); })
  );
  if (light.supports_temperature) {
    controls.appendChild(
      buildSlider(label(light), 'Warmth', 2900, 7000, Number(light.temperature_k || 2900), 'K',
        function (next) { applyLight(light, { temperature_k: next }); })
    );
  } else {
    const unavailable = document.createElement('div');
    unavailable.className = 'light-unavailable';
    unavailable.textContent = 'Color temperature unavailable';
    controls.appendChild(unavailable);
  }
  row.appendChild(controls);

  return row;
}

// A Tuya light is the plug switch row (name + toggle, rename modal) plus, for a
// dimmer, the Elgato row's Brightness slider wrapping onto its own line (#870).
function buildTuyaLightRow(device) {
  const row = buildPlugRow(device);
  if (!device.reachable || !device.has_brightness) return row;
  row.classList.add('light-row');
  const name = device.display_name || device.name || 'Light';
  const controls = document.createElement('div');
  controls.className = 'light-controls';
  controls.appendChild(
    buildSlider(name, 'Brightness', 1, 100, Number(device.brightness_pct || 1), '%',
      function (next) { setTuyaBrightness(device, next); })
  );
  row.appendChild(controls);
  return row;
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
  openDialog(els.lightDialog);
  els.lightDisplayName.focus();
}

function closeLightDetail() {
  state.selectedLightId = null;
  closeDialog(els.lightDialog);
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
    reportActionFailure(exc, 'Failed to save name');
  }
}

function showLightsState(iconName, message, retry) {
  els.lightsList.innerHTML = '';
  const options = retry ? {
    actionLabel: 'Retry',
    onAction: function () { loadLights(); },
  } : null;
  els.lightsList.appendChild(emptyStateEl(iconName, message, options));
}

// Count badge in the card summary, mirroring the Plugs/Blinds cards. Unlike
// those, the Lights card is never hidden when empty: its empty/error state and
// Retry action live inside its own body, so hiding the card would strand them.
function setLightsCount(n) {
  if (!els.lightsCount) return;
  els.lightsCount.textContent = String(n);
  els.lightsCount.hidden = n === 0;
}

export function renderLights() {
  els.lightsList.innerHTML = '';
  els.lightsList.dataset.state = lightsView.state;
  els.lightsList.setAttribute('aria-busy', lightsView.state === 'loading' ? 'true' : 'false');
  const tuyaLights = state.tuyaLights;
  setLightsCount(state.lights.length + tuyaLights.length);
  if (!state.lights.length && !tuyaLights.length) {
    updateBulkControls();
    if (lightsView.state === 'loading') {
      showLightsState('refresh-cw', 'Reading Elgato lights…', false);
      els.lightsNote.hidden = true;
    } else if (lightsView.state === 'error') {
      showLightsState('lightbulb', 'Lights unavailable', true);
      els.lightsNote.hidden = false;
      els.lightsNote.textContent = LIGHTS_UNAVAILABLE_COPY;
    } else {
      showLightsState('lightbulb', 'No lights configured or discovered', true);
      els.lightsNote.hidden = false;
      els.lightsNote.textContent =
        'Add ELGATO_LIGHT_HOSTS=host[:9123] to .env or enable Bonjour/mDNS.';
    }
    return;
  }
  if (!state.lights.length) {
    // Only Tuya lights to show: the Elgato side has nothing to list, so its
    // setup hint would be noise — but a failed Elgato read still says so.
    els.lightsNote.hidden = lightsView.state !== 'error';
    els.lightsNote.textContent = LIGHTS_UNAVAILABLE_COPY;
  } else if (lightsView.state === 'stale' && lightsView.liveUnavailable) {
    els.lightsNote.hidden = false;
    els.lightsNote.textContent = lightsView.lastUpdatedLabel() + ' · live data unavailable';
  } else if (isSnapshotRestored('lights')) {
    els.lightsNote.hidden = false;
    els.lightsNote.textContent = snapshotLabel('lights');
  } else {
    els.lightsNote.hidden = true;
  }
  const rows = state.lights.map(function (light) {
    return { name: label(light), build: function () { return buildLightRow(light); } };
  }).concat(tuyaLights.map(function (device) {
    return {
      name: device.display_name || device.name || '',
      build: function () { return buildTuyaLightRow(device); },
    };
  }));
  rows.sort(function (a, b) { return a.name.localeCompare(b.name); });
  rows.forEach(function (row) { els.lightsList.appendChild(row.build()); });
  updateBulkControls();
}

export async function loadLights() {
  if (!state.lights.length) {
    lightsView.set('loading', { liveUnavailable: false });
    renderLights();
  }
  try {
    const body = await jsonApi('/api/lights');
    reportFetchOk('lights');
    saveSnapshot('lights', body);
    state.lights = (body && body.lights) || [];
    lightsView.set(state.lights.length ? 'ready' : 'empty', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderLights();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markLightsFailure();
  }
}

export function restoreLightsSnapshot() {
  const body = restoreSnapshot('lights');
  if (!body) return;
  state.lights = (body && body.lights) || [];
  lightsView.set(state.lights.length ? 'stale' : 'empty', {
    updatedAt: state.snapshotUpdatedAt.lights,
    liveUnavailable: false,
  });
  renderLights();
}

const schedule = createPoller(loadLights);

export function onLightsTab(tab) {
  if (tab === 'iot') {
    loadLights();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}

export function wireLightControls() {
  document.addEventListener('plugs:rendered', renderLights);
  if (els.lightsRefresh) {
    els.lightsRefresh.addEventListener('click', async function () {
      els.lightsRefresh.disabled = true;
      try {
        const body = await jsonApi('/api/lights/refresh', { method: 'POST' });
        reportFetchOk('lights');
        saveSnapshot('lights', body);
        state.lights = (body && body.lights) || [];
        lightsView.set(state.lights.length ? 'ready' : 'empty', {
          updatedAt: new Date(),
          liveUnavailable: false,
        });
        renderLights();
        toast('Lights refreshed', 'success');
      } catch (exc) {
        if (!isAuthRequired(exc)) {
          markLightsFailure();
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
