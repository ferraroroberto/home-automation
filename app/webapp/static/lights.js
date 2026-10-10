/* Lights controller (Devices tab).
 *
 * Reads GET /api/lights and writes POST /api/lights/{id}. Polling is tab-aware
 * like Plugs: the LAN read runs only while the Devices tab is open.
 *
 * The group also lists the Tuya lights (#181). Those are read by plugs.js as
 * part of GET /api/tuya and handed over as state.tuyaLights on every Plugs
 * render (the 'plugs:rendered' event); their on/off and dimming reuse the
 * Tuya write path, so there is one Tuya read and one Tuya write path.
 *
 * Every light is the shared row (row.js, #884): the bulb, the name, one meta
 * line (brightness and warmth while on), the power switch. Tapping the row
 * opens the light sheet (decision 5 of #872): power, brightness and warmth,
 * each applied as it changes, and a link to the staged name / details dialog
 * (lightDialog for an Elgato light, the plug dialog for a Tuya one). */

'use strict';

import { state, els, toast, reportFetchOk } from './state.js';
import { createViewState, markTabFailure } from './view-state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { emptyStateEl } from './empty-state.js';
import { createPoller } from './poll.js';
import { toggleMarkup, setToggleState } from './toggle.js';
import { closeDialog, openDialog } from './dialog.js';
import { openPlugDetail, setTuyaBrightness, toggleSwitch, tuyaSwitch } from './plugs.js';
import { friendlyError } from './format.js';
import { rowEl } from './row.js';
import { chipEl } from './chip.js';
import { sheet } from './sheet.js';

const POLL_MS = 15_000;
const LIGHTS_UNAVAILABLE_COPY =
  'Live light data is unavailable. Check the light connection, then retry.';

const lightsView = createViewState('lights');

function label(light) {
  return light.display_name || light.name || light.light_id || 'Elgato light';
}

function tuyaLabel(device) {
  return device.display_name || device.name || 'Light';
}

function lightById(lightId) {
  return state.lights.find(function (light) { return light.light_id === lightId; });
}

function tuyaLightById(deviceId) {
  return state.tuyaLights.find(function (device) { return device.device_id === deviceId; });
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
// never per pixel — shared by the Elgato and Tuya lights (#870).
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

// ------------------------------------------------------------- the rows
function powerSwitch(on, name, onClick) {
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle' + (on ? ' on' : '');
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', on ? 'true' : 'false');
  toggle.setAttribute('aria-label', 'Power ' + name);
  toggle.innerHTML = toggleMarkup(on);
  toggle.addEventListener('click', onClick);
  return toggle;
}

// The meta line while on: brightness, then warmth where the light has it.
function elgatoMeta(light) {
  if (light.on !== true) return 'Off';
  const parts = [];
  if (light.brightness != null) parts.push(Math.round(Number(light.brightness)) + '%');
  if (light.supports_temperature && light.temperature_k) parts.push(light.temperature_k + ' K');
  return parts.join(' · ') || 'On';
}

function tuyaMeta(device) {
  if (device.switch_on !== true) return 'Off';
  if (device.has_brightness && device.brightness_pct != null) {
    return Math.round(Number(device.brightness_pct)) + '%';
  }
  return 'On';
}

// An unreachable light should be there and is not: the down badge and an
// attention chip, never its connection error (it carries the address, #879).
function lightRow(opts) {
  const row = rowEl({
    className: 'light-row',
    glyph: 'lightbulb',
    badge: opts.reachable ? null : 'down',
    title: opts.name,
    meta: opts.reachable ? opts.meta : '',
    chip: opts.reachable
      ? (opts.hidden ? chipEl('Hidden', null, 'device-hidden-chip') : null)
      : chipEl('Offline', 'attention', 'light-offline'),
    onOpen: opts.onOpen,
    trail: opts.reachable ? opts.trail : null,
  });
  if (!opts.reachable) row.classList.add('is-unavailable');
  return row;
}

function buildLightRow(light) {
  const name = label(light);
  const row = lightRow({
    name: name,
    reachable: light.reachable,
    meta: elgatoMeta(light),
    onOpen: function (btn) { openLightSheet({ kind: 'elgato', id: light.light_id }, btn); },
    trail: powerSwitch(light.on === true, name, function () {
      applyLight(light, { on: light.on !== true });
    }),
  });
  row.dataset.lightId = light.light_id;
  return row;
}

function buildTuyaLightRow(device) {
  const row = lightRow({
    name: tuyaLabel(device),
    reachable: device.reachable,
    hidden: device.hidden,
    meta: tuyaMeta(device),
    onOpen: function (btn) { openLightSheet({ kind: 'tuya', id: device.device_id }, btn); },
    trail: device.has_switch ? tuyaSwitch(device, tuyaLabel(device)) : null,
  });
  row.dataset.deviceId = device.device_id;
  return row;
}

// ------------------------------------------------------- the light sheet
// Which light the sheet shows: {kind: 'elgato' | 'tuya', id}. Its controls
// are rebuilt on open and when the light's reachability or dimming changes,
// never on a plain poll, so a poll can't pull a slider out from under a
// finger; the power switch follows every render.
let sheetLight = null;
let sheetShape = '';

const lightSheet = sheet(els.lightSheet, {
  model: 'instant',
  closeButton: els.lightSheetClose,
  doneButton: els.lightSheetDone,
  fallbackFocus: function () {
    if (!sheetLight) return null;
    const attr = sheetLight.kind === 'elgato' ? 'data-light-id' : 'data-device-id';
    return els.lightsList.querySelector('.light-row[' + attr + '="' + CSS.escape(sheetLight.id) + '"] .action-row-main');
  },
  onClose: function () {
    sheetLight = null;
    sheetShape = '';
  },
});

function sheetEntity() {
  if (!sheetLight) return null;
  return sheetLight.kind === 'elgato' ? lightById(sheetLight.id) : tuyaLightById(sheetLight.id);
}

function renderLightSheet(force) {
  const entity = sheetEntity();
  if (!entity || !els.lightSheet) return;
  const elgato = sheetLight.kind === 'elgato';
  const name = elgato ? label(entity) : tuyaLabel(entity);
  const on = elgato ? entity.on === true : entity.switch_on === true;
  const reachable = !!entity.reachable;
  els.lightSheetName.textContent = name;
  els.lightSheetOffline.hidden = reachable;
  setToggleState(els.lightSheetPower, on);
  els.lightSheetPower.disabled = !reachable || (!elgato && !entity.has_switch);
  els.lightSheetPower.setAttribute('aria-label', 'Power ' + name);
  els.lightSheetEditMeta.textContent = elgato
    ? (entity.product_name || 'Product, firmware')
    : 'Name, hidden';

  const shape = [reachable, elgato ? entity.supports_temperature : entity.has_brightness].join('|');
  if (!force && shape === sheetShape) return;
  sheetShape = shape;
  els.lightSheetControls.innerHTML = '';
  if (!reachable) return;
  if (elgato) {
    els.lightSheetControls.appendChild(
      buildSlider(name, 'Brightness', 3, 100, Number(entity.brightness || 3), '%',
        function (next) { applyLight(lightById(entity.light_id) || entity, { brightness: next }); })
    );
    if (entity.supports_temperature) {
      els.lightSheetControls.appendChild(
        buildSlider(name, 'Warmth', 2900, 7000, Number(entity.temperature_k || 2900), 'K',
          function (next) { applyLight(lightById(entity.light_id) || entity, { temperature_k: next }); })
      );
    } else {
      const none = document.createElement('p');
      none.className = 'muted small light-unavailable';
      none.textContent = 'Brightness only';
      els.lightSheetControls.appendChild(none);
    }
  } else if (entity.has_brightness) {
    els.lightSheetControls.appendChild(
      buildSlider(name, 'Brightness', 1, 100, Number(entity.brightness_pct || 1), '%',
        function (next) { setTuyaBrightness(tuyaLightById(entity.device_id) || entity, next); })
    );
  }
}

function openLightSheet(which, trigger) {
  sheetLight = which;
  if (!sheetEntity()) { sheetLight = null; return; }
  renderLightSheet(true);
  lightSheet.open(trigger);
}

function onSheetPower() {
  const entity = sheetEntity();
  if (!entity) return;
  if (sheetLight.kind === 'elgato') applyLight(entity, { on: entity.on !== true });
  else toggleSwitch(entity, els.lightSheetPower);
}

function onSheetEdit() {
  const entity = sheetEntity();
  if (!entity) return;
  if (sheetLight.kind === 'elgato') openLightDetail(entity.light_id);
  else openPlugDetail(entity.device_id, els.lightSheetEdit);
}

// ------------------------------------------- Elgato name + details dialog
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

// ------------------------------------------------------------- render
function showLightsState(iconName, message, retry) {
  els.lightsList.innerHTML = '';
  const options = retry ? {
    actionLabel: 'Retry',
    onAction: function () { loadLights(); },
  } : null;
  els.lightsList.appendChild(emptyStateEl(iconName, message, options));
}

// The group's meta: how many are on. The group is never hidden when empty:
// its empty/error state and Retry live in its own body.
function setLightsCount(onCount, total) {
  if (!els.lightsCount) return;
  els.lightsCount.textContent = onCount ? onCount + ' on' : 'All off';
  els.lightsCount.hidden = total === 0;
}

export function renderLights() {
  els.lightsList.innerHTML = '';
  els.lightsList.dataset.state = lightsView.state;
  els.lightsList.setAttribute('aria-busy', lightsView.state === 'loading' ? 'true' : 'false');
  const tuyaLights = state.tuyaLights;
  const onCount = state.lights.filter(function (l) { return l.reachable && l.on === true; }).length +
    tuyaLights.filter(function (d) { return d.reachable && d.switch_on === true; }).length;
  setLightsCount(onCount, state.lights.length + tuyaLights.length);
  if (!state.lights.length && !tuyaLights.length) {
    updateBulkControls();
    if (lightsView.state === 'loading') {
      showLightsState('refresh-cw', 'Reading lights…', false);
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
    renderLightSheet(false);
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
    return { name: tuyaLabel(device), build: function () { return buildTuyaLightRow(device); } };
  }));
  rows.sort(function (a, b) { return a.name.localeCompare(b.name); });
  const list = document.createElement('ul');
  list.className = 'action-rows';
  rows.forEach(function (row) { list.appendChild(row.build()); });
  els.lightsList.appendChild(list);
  updateBulkControls();
  renderLightSheet(false);
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
  if (els.lightSheetPower) els.lightSheetPower.addEventListener('click', onSheetPower);
  if (els.lightSheetEdit) els.lightSheetEdit.addEventListener('click', onSheetEdit);
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
