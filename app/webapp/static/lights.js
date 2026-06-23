/* Elgato Lights tab controller.
 *
 * Reads GET /api/lights and writes POST /api/lights/{id}. Polling is tab-aware
 * like Plugs: the LAN read runs only while the Lights tab is open. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';

const POLL_MS = 15_000;
let lightsTimer = null;

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

async function applyLight(light, patch) {
  try {
    const updated = await jsonApi('/api/lights/' + encodeURIComponent(light.light_id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    state.lights = state.lights.map(function (item) {
      return item.light_id === updated.light_id ? Object.assign({}, item, updated) : item;
    });
    renderLights();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function applyAllLights(on) {
  const reachable = state.lights.filter(function (light) { return light.reachable; });
  if (!reachable.length) {
    toast('No reachable lights', 'error');
    return;
  }
  let failures = 0;
  for (const light of reachable) {
    try {
      const updated = await jsonApi('/api/lights/' + encodeURIComponent(light.light_id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: on }),
      });
      state.lights = state.lights.map(function (item) {
        return item.light_id === updated.light_id ? Object.assign({}, item, updated) : item;
      });
    } catch (exc) {
      failures += 1;
    }
  }
  await loadLights();
  if (failures) toast(failures + ' light command(s) failed', 'error');
  else toast(on ? 'Lights on' : 'Lights off', 'good');
}

function buildSlider(light, key, min, max, value, suffix) {
  const row = document.createElement('div');
  row.className = 'light-control-row';
  const valueText = suffix === 'K' ? String(value) + ' K' : String(value) + suffix;
  row.innerHTML =
    '<span class="light-control-head"><span>' + key + '</span><span class="light-control-value">' + valueText + '</span></span>';
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
  const output = row.querySelector('.light-control-value');
  const paintSlider = function (next) {
    const pct = ((Number(next) - min) / (max - min)) * 100;
    slider.style.setProperty('--light-slider-pct', Math.max(0, Math.min(100, pct)) + '%');
  };
  const sync = function (next) {
    slider.value = String(next);
    number.value = String(next);
    output.textContent = suffix === 'K' ? next + ' K' : next + suffix;
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
  controls.appendChild(slider);
  controls.appendChild(number);
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
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (on ? 'ON' : 'OFF') + '</span>';
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

  const foot = document.createElement('div');
  foot.className = 'light-foot muted small';
  foot.textContent = light.supports_temperature
    ? 'Elgato ' + light.temperature + ' mired · ' + fmtTemperature(light)
    : 'Elgato light · brightness only';
  card.appendChild(foot);

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
  const displayName = els.lightDisplayName.value.trim();
  try {
    await jsonApi('/api/lights/' + encodeURIComponent(lightId) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName }),
    });
    state.lights = state.lights.map(function (light) {
      return light.light_id === lightId ? Object.assign({}, light, { display_name: displayName || null }) : light;
    });
    const light = lightById(lightId);
    if (light) els.lightDetailName.textContent = label(light);
    renderLights();
    toast('Name saved', 'good');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

export function renderLights() {
  els.lightsGrid.innerHTML = '';
  if (!state.lights.length) {
    els.lightsNote.hidden = false;
    els.lightsNote.textContent =
      'No Elgato lights found. Add ELGATO_LIGHT_HOSTS=host[:9123] to .env or enable Bonjour/mDNS.';
    return;
  }
  els.lightsNote.hidden = true;
  const sorted = state.lights.slice().sort(function (a, b) {
    return label(a).localeCompare(label(b));
  });
  sorted.forEach(function (light) { els.lightsGrid.appendChild(buildCard(light)); });
}

export async function loadLights() {
  try {
    const body = await jsonApi('/api/lights');
    reportFetchOk('lights');
    state.lights = (body && body.lights) || [];
    renderLights();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('lights', exc, 'lights');
    state.lights = [];
    els.lightsGrid.innerHTML = '';
    els.lightsNote.hidden = false;
    els.lightsNote.textContent = exc.message || 'Failed to load Elgato lights.';
  }
}

function schedule(ms) {
  if (lightsTimer) clearInterval(lightsTimer);
  lightsTimer = ms > 0 ? setInterval(loadLights, ms) : null;
}

export function onLightsTab(tab) {
  if (tab === 'lights') {
    loadLights();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}

export function wireLightControls() {
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
  els.lightDisplayName.addEventListener('blur', saveLightName);
  els.lightDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.lightDisplayName.blur(); }
  });
}
