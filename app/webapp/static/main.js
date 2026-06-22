/* Home Automation — entry module: boots the dashboard and wires events.
 *
 * Loaded by index.html as <script type="module">. Renders one card per
 * unit with the everyday controls inline (power / target / fan + room
 * readout); secondary settings (mode + both vanes) live in a per-unit
 * detail modal. Each write hits POST /api/units/{id} and re-renders only
 * that card from the read-back response.
 */

'use strict';

import {
  state,
  els,
  toast,
  modeIcon,
  tokenFromUrl,
  writeToken,
  THEME_KEY,
} from './state.js';
import { icon } from './icons.js';
import { jsonApi, hideLogin } from './api.js';
import { setTab, wireTabs, onTabChange, initialTab } from './tabs.js';
import {
  loadEnergy,
  wireEnergyControls,
  onEnergyTab,
  restyleEnergyCharts,
} from './energy.js';
import { onPlugsTab, wirePlugsToggle, wirePlugDetail } from './plugs.js';
import { onSecurityTab, wireZoneDetail, wireSecurityHiddenToggle } from './security.js';
import { startWeatherPolling } from './weather.js';

const DEFAULT_RANGE = [16, 31];
const ASSET_HASH_KEY = 'home-automation.assetHash';
const ASSET_RELOAD_KEY = 'home-automation.assetReloadedFor';
let currentScheduleEntries = [];

// --------------------------------------------------------------- helpers
function unitById(id) {
  return state.units.find(function (u) { return u.unit_id === id; });
}

function tempRange(unit) {
  let rng = unit.temp_ranges && unit.temp_ranges[unit.operation_mode];
  if (!rng && unit.temp_ranges) {
    const vals = Object.values(unit.temp_ranges);
    if (vals.length) rng = vals[0];
  }
  return rng && rng.length === 2 ? rng : DEFAULT_RANGE;
}

function fmtTemp(v) {
  return v == null ? '—' : Number(v).toFixed(1) + '°';
}

function fanLabel(v) {
  const labels = { One: '1', Two: '2', Three: '3', Four: '4', Five: '5' };
  return labels[v] || v || '—';
}

function ruleTargetForMode(rule, mode) {
  if (!rule || rule.enabled !== true) return null;
  if (mode === 'Cool' || mode === 'Dry') return rule.cool_target == null ? null : rule.cool_target;
  if (mode === 'Heat') return rule.heat_target == null ? null : rule.heat_target;
  return null;
}

function activeRuleTarget(unit) {
  const rule = unit.temperature_rule || {};
  return rule.enabled && rule.active_target != null ? rule.active_target : null;
}

function scheduleCount(unit) {
  const sched = unit.schedule || {};
  if (Number.isFinite(Number(sched.count))) return Number(sched.count);
  return sched.enabled === true ? 1 : 0;
}

function hasSchedule(unit) {
  return scheduleCount(unit) > 0;
}

// --------------------------------------------------- write + re-render
async function applyControl(unitId, patch) {
  try {
    const updated = await jsonApi('/api/units/' + encodeURIComponent(unitId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    state.units = state.units.map(function (u) {
      return u.unit_id === updated.unit_id ? updated : u;
    });
    rerenderCard(updated.unit_id);
    renderAcSummary();
    if (state.selectedId === updated.unit_id) populateDetail(updated);
    toast('Saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

// ------------------------------------------------------------- card DOM
function buildCard(unit) {
  const card = document.createElement('article');
  card.className = 'card unit-card';
  card.dataset.unitId = unit.unit_id;
  renderCardInto(card, unit);
  return card;
}

function renderCardInto(card, unit) {
  const on = unit.power === true;
  card.classList.toggle('is-off', !on);
  const [tmin, tmax] = tempRange(unit).map(Number);
  const step = Number(unit.temp_step) || 0.5;

  card.innerHTML = '';

  // --- Top band: name (opens modal) + the action controls beside it. ---
  const top = document.createElement('div');
  top.className = 'unit-top';
  card.appendChild(top);

  // Header — mode icon + name, opens the detail modal. Kept a sibling of
  // the controls so tapping power/fan does not also open the modal.
  const header = document.createElement('button');
  header.type = 'button';
  header.className = 'unit-header';
  header.title = 'Open settings';
  const schedCount = scheduleCount(unit);
  header.innerHTML =
    '<span class="unit-mode-icon">' + icon(modeIcon(unit.operation_mode)) + '</span>' +
    '<span class="unit-name"></span>' +
    (schedCount ? '<span class="unit-schedule-badge" title="' + schedCount + ' schedule' + (schedCount === 1 ? '' : 's') + '">' +
      icon('clock', 'unit-schedule-icon') + (schedCount > 1 ? '<span>' + schedCount + '</span>' : '') + '</span>' : '');
  header.querySelector('.unit-name').textContent = displayLabel(unit) || 'Unit';
  header.addEventListener('click', function () { openDetail(unit.unit_id); });
  top.appendChild(header);

  // Power toggle (ESTADO).
  const power = document.createElement('button');
  power.type = 'button';
  power.className = 'toggle' + (on ? ' on' : '');
  power.setAttribute('role', 'switch');
  power.setAttribute('aria-checked', on ? 'true' : 'false');
  power.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
    (on ? 'ON' : 'OFF') + '</span>';
  power.addEventListener('click', function () {
    applyControl(unit.unit_id, { power: !on });
  });

  // Fan speed — labelled in the top band so it is clear what the compact
  // selector controls. Native <select> options stay text-only; numbered speeds
  // render as 1–5 while preserving the API values (One/Two/etc.).
  if (unit.fan_speeds && unit.fan_speeds.length) {
    const fan = document.createElement('label');
    fan.className = 'unit-fan-control';
    fan.innerHTML = '<span class="unit-fan-label">Fan level</span>';
    const sel = document.createElement('select');
    sel.className = 'select-native unit-fan';
    sel.setAttribute('aria-label', 'Fan speed');
    unit.fan_speeds.forEach(function (f) {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = fanLabel(f);
      if (f === unit.fan_speed) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', function () {
      applyControl(unit.unit_id, { fan_speed: sel.value });
    });
    fan.appendChild(sel);
    top.appendChild(fan);
  }

  // Power toggle appended last → right edge of the top band (issue #80).
  top.appendChild(power);

  // --- Readings band: room temperature + target stepper. ---
  const readings = document.createElement('div');
  readings.className = 'unit-readings';
  card.appendChild(readings);

  // Room temperature readout (TEMP. AMBIENTE).
  const room = document.createElement('div');
  room.className = 'unit-room';
  room.innerHTML =
    '<span class="label">Room</span>' +
    '<span class="value">' + fmtTemp(unit.room_temperature) + '</span>';
  readings.appendChild(room);

  const ruleTarget = activeRuleTarget(unit);
  if (ruleTarget != null) {
    const rule = document.createElement('div');
    rule.className = 'unit-rule-target';
    rule.innerHTML =
      '<span class="label">Rule</span>' +
      '<span class="value">' + icon('thermometer', 'unit-rule-icon') + fmtTemp(ruleTarget) + '</span>';
    readings.appendChild(rule);
  }

  // Target temperature (AJUSTAR A) with steppers.
  const target = document.createElement('div');
  target.className = 'unit-target';
  const cur = unit.set_temperature == null ? tmin : Number(unit.set_temperature);
  target.innerHTML =
    '<span class="label">Set to</span>' +
    '<div class="stepper">' +
    '  <button type="button" class="step minus" aria-label="Lower">−</button>' +
    '  <span class="target-value">' + fmtTemp(cur) + '</span>' +
    '  <button type="button" class="step plus" aria-label="Raise">+</button>' +
    '</div>';
  const setTo = function (v) {
    const clamped = Math.min(Math.max(v, tmin), tmax);
    if (clamped === cur) return;
    applyControl(unit.unit_id, { set_temperature: clamped });
  };
  target.querySelector('.minus').addEventListener('click', function () {
    setTo(Math.round((cur - step) * 10) / 10);
  });
  target.querySelector('.plus').addEventListener('click', function () {
    setTo(Math.round((cur + step) * 10) / 10);
  });
  readings.appendChild(target);
}

function rerenderCard(unitId) {
  const card = els.grid.querySelector('[data-unit-id="' + CSS.escape(unitId) + '"]');
  const unit = unitById(unitId);
  if (card && unit) renderCardInto(card, unit);
}

function displayLabel(unit) {
  return unit.display_name || unit.name || '';
}

function renderAll() {
  els.grid.innerHTML = '';
  const sorted = state.units.slice().sort(function (a, b) {
    return displayLabel(a).localeCompare(displayLabel(b));
  });
  sorted.forEach(function (u) { els.grid.appendChild(buildCard(u)); });
}

// ----------------------------------------------------------- detail modal
function fillSelect(sel, options, current) {
  sel.innerHTML = '';
  options.forEach(function (o) {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o;
    if (o === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

function populateDetail(unit) {
  els.detailName.textContent = displayLabel(unit) || 'Unit';
  els.detailDisplayName.value = unit.display_name || '';
  els.detailDisplayName.placeholder = unit.name || 'Custom label…';

  fillSelect(els.detailMode, unit.operation_modes || [], unit.operation_mode);

  els.detailFanSpeedRow.hidden = !unit.fan_speeds || !unit.fan_speeds.length;
  if (unit.fan_speeds && unit.fan_speeds.length) {
    els.detailFanSpeed.innerHTML = '';
    unit.fan_speeds.forEach(function (f) {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = fanLabel(f);
      if (f === unit.fan_speed) opt.selected = true;
      els.detailFanSpeed.appendChild(opt);
    });
  }

  els.detailVaneVerticalRow.hidden = !unit.has_vane_vertical;
  if (unit.has_vane_vertical) {
    fillSelect(els.detailVaneVertical, unit.vane_vertical_options || [], unit.vane_vertical);
  }
  els.detailVaneHorizontalRow.hidden = !unit.has_vane_horizontal;
  if (unit.has_vane_horizontal) {
    fillSelect(els.detailVaneHorizontal, unit.vane_horizontal_options || [], unit.vane_horizontal);
  }

  currentScheduleEntries = [];
  renderScheduleList(unit);
}

function newScheduleId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return 'sched-' + window.crypto.randomUUID().slice(0, 8);
  }
  return 'sched-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
}

function normalizeScheduleEntries(body) {
  if (Array.isArray(body)) return body;
  if (body && Array.isArray(body.entries)) return body.entries;
  if (body && (body.enabled === true || body.time || body.operation_mode || body.set_temperature != null)) {
    return [body];
  }
  return [];
}

function scheduleDefaults(unit) {
  return {
    id: newScheduleId(),
    enabled: true,
    time: '08:00',
    power: true,
    operation_mode: unit.operation_mode || null,
    set_temperature: unit.set_temperature == null ? null : unit.set_temperature,
    fan_speed: unit.fan_speed || null,
    vane_vertical_direction: unit.has_vane_vertical ? (unit.vane_vertical || null) : null,
    vane_horizontal_direction: unit.has_vane_horizontal ? (unit.vane_horizontal || null) : null,
  };
}

function optionHtml(options, current) {
  return (options || []).map(function (o) {
    const selected = o === current ? ' selected' : '';
    return '<option value="' + o + '"' + selected + '>' + o + '</option>';
  }).join('');
}

function renderScheduleList(unit) {
  if (!els.schedList) return;
  els.schedList.innerHTML = '';
  if (!currentScheduleEntries.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small schedule-empty';
    empty.textContent = 'No schedules yet.';
    els.schedList.appendChild(empty);
    return;
  }

  currentScheduleEntries.forEach(function (entry, idx) {
    const card = document.createElement('div');
    card.className = 'schedule-entry' + (entry.power === false ? ' is-off-entry' : '');
    card.dataset.index = String(idx);
    card.innerHTML =
      '<div class="schedule-entry-head">' +
      '  <label class="schedule-enabled"><input type="checkbox" class="checkbox-native sched-entry-enabled"' + (entry.enabled ? ' checked' : '') + '> <span>Enabled</span></label>' +
      '  <input type="time" class="input-native sched-entry-time" value="' + (entry.time || '08:00') + '">' +
      '  <select class="select-native sched-entry-power"><option value="true"' + (entry.power === false ? '' : ' selected') + '>On</option><option value="false"' + (entry.power === false ? ' selected' : '') + '>Off</option></select>' +
      '  <button type="button" class="schedule-delete" aria-label="Delete schedule">×</button>' +
      '</div>' +
      '<div class="schedule-profile"' + (entry.power === false ? ' hidden' : '') + '>' +
      '  <label class="row"><span>Mode</span><select class="select-native sched-entry-mode">' + optionHtml(unit.operation_modes || [], entry.operation_mode || unit.operation_mode) + '</select></label>' +
      '  <label class="row"><span>Target temp (°C)</span><input type="number" step="0.5" min="10" max="31" class="input-native sched-entry-temp" placeholder="—" value="' + (entry.set_temperature == null ? '' : entry.set_temperature) + '"></label>' +
      '  <label class="row"><span>Fan</span><select class="select-native sched-entry-fan">' + optionHtml(unit.fan_speeds || [], entry.fan_speed || unit.fan_speed) + '</select></label>' +
      (unit.has_vane_vertical ? '  <label class="row"><span>Vane — vertical</span><select class="select-native sched-entry-vv">' + optionHtml(unit.vane_vertical_options || [], entry.vane_vertical_direction || unit.vane_vertical) + '</select></label>' : '') +
      (unit.has_vane_horizontal ? '  <label class="row"><span>Vane — horizontal</span><select class="select-native sched-entry-vh">' + optionHtml(unit.vane_horizontal_options || [], entry.vane_horizontal_direction || unit.vane_horizontal) + '</select></label>' : '') +
      '</div>';

    const saveFromCard = function () {
      entry.enabled = card.querySelector('.sched-entry-enabled').checked;
      entry.time = card.querySelector('.sched-entry-time').value || '08:00';
      entry.power = card.querySelector('.sched-entry-power').value !== 'false';
      const profile = card.querySelector('.schedule-profile');
      profile.hidden = entry.power === false;
      card.classList.toggle('is-off-entry', entry.power === false);
      const mode = card.querySelector('.sched-entry-mode');
      const temp = card.querySelector('.sched-entry-temp');
      const fan = card.querySelector('.sched-entry-fan');
      const vv = card.querySelector('.sched-entry-vv');
      const vh = card.querySelector('.sched-entry-vh');
      entry.operation_mode = mode ? mode.value || null : null;
      entry.set_temperature = temp ? numOrNull(temp) : null;
      entry.fan_speed = fan ? fan.value || null : null;
      entry.vane_vertical_direction = vv ? vv.value || null : null;
      entry.vane_horizontal_direction = vh ? vh.value || null : null;
      saveSchedules();
    };

    card.querySelector('.sched-entry-enabled').addEventListener('change', saveFromCard);
    card.querySelector('.sched-entry-time').addEventListener('blur', saveFromCard);
    card.querySelector('.sched-entry-power').addEventListener('change', saveFromCard);
    card.querySelector('.schedule-delete').addEventListener('click', function () {
      currentScheduleEntries.splice(idx, 1);
      renderScheduleList(unit);
      saveSchedules();
    });
    card.querySelectorAll('.sched-entry-mode, .sched-entry-fan, .sched-entry-vv, .sched-entry-vh').forEach(function (el) {
      el.addEventListener('change', saveFromCard);
    });
    const tempInput = card.querySelector('.sched-entry-temp');
    if (tempInput) tempInput.addEventListener('blur', saveFromCard);
    els.schedList.appendChild(card);
  });
}

// Load the saved rule + schedules for the open unit and fill the two sections.
// Failures stay quiet (auth overlay handles 401) — the fields just keep their
// defaults.
async function loadAutomation(unitId) {
  try {
    const rule = await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/rule');
    els.ruleEnabled.checked = rule.enabled === true;
    els.ruleCoolTarget.value = rule.cool_target == null ? '' : rule.cool_target;
    els.ruleHeatTarget.value = rule.heat_target == null ? '' : rule.heat_target;
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
  }
  try {
    const sched = await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/schedule');
    currentScheduleEntries = normalizeScheduleEntries(sched).map(function (entry, idx) {
      return Object.assign({ id: 'schedule-' + (idx + 1), enabled: true, time: '08:00', power: true }, entry);
    });
    const unit = unitById(unitId);
    if (unit) renderScheduleList(unit);
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
  }
}

function openDetail(unitId) {
  const unit = unitById(unitId);
  if (!unit) return;
  state.selectedId = unitId;
  populateDetail(unit);
  loadAutomation(unitId);
  if (typeof els.detail.showModal === 'function') els.detail.showModal();
  else els.detail.setAttribute('open', '');
}

function closeDetail() {
  state.selectedId = null;
  if (typeof els.detail.close === 'function') els.detail.close();
  else els.detail.removeAttribute('open');
}

// --------------------------------------------------- build identity
function fmtBuildTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 16);
  const pad = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

async function fetchVersion() {
  // Visible proof of which build the PWA is running — confirms a tray
  // restart actually picked up new code. Uses jsonApi so the bearer token
  // is attached (/api/version is auth-gated like the rest of the API).
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const assetHash = body.asset_hash || '';
    const previousHash = localStorage.getItem(ASSET_HASH_KEY) || '';
    if (
      assetHash && previousHash && previousHash !== assetHash &&
      sessionStorage.getItem(ASSET_RELOAD_KEY) !== assetHash
    ) {
      // iOS standalone PWAs can cling to an old shell even with stamped asset
      // URLs. Once a freshly-loaded JS has this guard, future deploys get one
      // automatic reload instead of needing a home-screen reinstall.
      localStorage.setItem(ASSET_HASH_KEY, assetHash);
      sessionStorage.setItem(ASSET_RELOAD_KEY, assetHash);
      window.location.reload();
      return;
    }
    if (assetHash) localStorage.setItem(ASSET_HASH_KEY, assetHash);
    const ts = fmtBuildTime(body.built_at || '');
    els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
  } catch (_) {
    els.buildReadout.textContent = '';
  }
}

// ------------------------------------------------ read-only AC summary (Home)
function renderAcSummary() {
  els.acSummary.innerHTML = '';
  if (!state.units.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small ac-summary-empty';
    empty.textContent = 'No units.';
    els.acSummary.appendChild(empty);
    return;
  }
  const sorted = state.units.slice().sort(function (a, b) {
    return displayLabel(a).localeCompare(displayLabel(b));
  });
  sorted.forEach(function (u) {
    const on = u.power === true;
    const row = document.createElement('div');
    row.className = 'ac-line' + (on ? '' : ' is-off');

    const name = document.createElement('span');
    name.className = 'ac-line-name';
    name.innerHTML = icon(modeIcon(u.operation_mode), 'ac-line-icon');
    name.insertAdjacentText('beforeend', ' ' + (displayLabel(u) || 'Unit'));

    // Centred temperature column: room → target on top, mode · fan beneath, so
    // the readings line up down the card (issue #72).
    const center = document.createElement('span');
    center.className = 'ac-line-center';
    const room = fmtTemp(u.room_temperature);
    const target = fmtTemp(u.set_temperature);
    center.innerHTML =
      '<span class="ac-temp">' + room + ' → ' + target + '</span>' +
      '<span class="ac-meta">' + (u.operation_mode || '—') +
        (u.fan_speed ? ' · fan ' + fanLabel(u.fan_speed) : '') + '</span>';

    // Power toggle — the app's standard switch, actionable from Home (issue #72).
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle ac-line-toggle' + (on ? ' on' : '');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Power ' + (displayLabel(u) || 'unit'));
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (on ? 'ON' : 'OFF') + '</span>';
    toggle.addEventListener('click', function () {
      applyControl(u.unit_id, { power: !on });
    });

    row.appendChild(name);
    row.appendChild(center);
    row.appendChild(toggle);
    els.acSummary.appendChild(row);
  });
}

// --------------------------------------------------------------- boot
async function loadUnits() {
  try {
    const body = await jsonApi('/api/units');
    state.units = (body && body.units) || [];
    renderAll();
    renderAcSummary();
  } catch (exc) {
    // A 401 already surfaced the login overlay (api.js → showLogin); stay quiet.
    if (String(exc.message) === 'auth required') return;
    toast('Load failed: ' + (exc.message || exc), 'error');
  }
}

// --------------------------------------------------------------- wire up
async function saveDisplayName() {
  if (!state.selectedId) return;
  const newName = els.detailDisplayName.value.trim();
  try {
    await jsonApi('/api/units/' + encodeURIComponent(state.selectedId) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    state.units = state.units.map(function (u) {
      if (u.unit_id !== state.selectedId) return u;
      return Object.assign({}, u, { display_name: newName || null });
    });
    const unit = unitById(state.selectedId);
    if (unit) els.detailName.textContent = displayLabel(unit) || 'Unit';
    renderAll();
    renderAcSummary();
    toast('Name saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

// A number input → a float, or null when blank/invalid (clears the target).
function numOrNull(input) {
  const raw = (input.value || '').trim();
  if (raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

async function saveRule() {
  if (!state.selectedId) return;
  const payload = {
    enabled: els.ruleEnabled.checked,
    cool_target: numOrNull(els.ruleCoolTarget),
    heat_target: numOrNull(els.ruleHeatTarget),
  };
  try {
    await jsonApi('/api/units/' + encodeURIComponent(state.selectedId) + '/rule', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.units = state.units.map(function (u) {
      if (u.unit_id !== state.selectedId) return u;
      return Object.assign({}, u, {
        temperature_rule: {
          enabled: payload.enabled,
          active_target: ruleTargetForMode(payload, u.operation_mode),
        },
      });
    });
    rerenderCard(state.selectedId);
    renderAcSummary();
    toast('Rule saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save rule: ' + (exc.message || exc), 'error');
    }
  }
}

async function saveSchedules() {
  if (!state.selectedId) return;
  const payload = { entries: currentScheduleEntries };
  try {
    const saved = await jsonApi('/api/units/' + encodeURIComponent(state.selectedId) + '/schedule', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    currentScheduleEntries = normalizeScheduleEntries(saved).map(function (entry, idx) {
      return Object.assign({ id: 'schedule-' + (idx + 1), enabled: true, time: '08:00', power: true }, entry);
    });
    state.units = state.units.map(function (u) {
      if (u.unit_id !== state.selectedId) return u;
      return Object.assign({}, u, {
        schedule: {
          enabled: saved.enabled === true,
          count: Number(saved.count) || 0,
          next_time: saved.next_time || null,
          time: saved.time || saved.next_time || null,
        },
      });
    });
    rerenderCard(state.selectedId);
    renderAcSummary();
    toast('Schedules saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save schedules: ' + (exc.message || exc), 'error');
    }
  }
}

// --------------------------------------------------------------- theme toggle
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  // Show the glyph for the action: sun to switch to light, moon to switch to dark.
  const mark = icon(dark ? 'sun' : 'moon');
  // Two toggles share the state: the Settings one (other tabs) and the weather
  // tile one (Home, which has no Settings card) — keep both icons in sync (#72).
  els.themeBtn.innerHTML = mark;
  if (els.weatherThemeBtn) els.weatherThemeBtn.innerHTML = mark;
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
  restyleEnergyCharts();
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme !== 'dark');
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(stored ? stored === 'dark' : prefersDark);
})();

els.themeBtn.addEventListener('click', toggleTheme);
els.weatherThemeBtn.addEventListener('click', toggleTheme);

els.detailClose.addEventListener('click', closeDetail);
els.detail.addEventListener('click', function (ev) {
  if (ev.target === els.detail) closeDetail();  // backdrop click
});
els.detailDisplayName.addEventListener('blur', saveDisplayName);
els.detailDisplayName.addEventListener('keydown', function (ev) {
  if (ev.key === 'Enter') { ev.preventDefault(); els.detailDisplayName.blur(); }
});
els.detailMode.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { operation_mode: els.detailMode.value });
});
els.detailFanSpeed.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { fan_speed: els.detailFanSpeed.value });
});
els.detailVaneVertical.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { vane_vertical_direction: els.detailVaneVertical.value });
});
els.detailVaneHorizontal.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { vane_horizontal_direction: els.detailVaneHorizontal.value });
});

// Temperature rule — save on any change; number inputs also save on blur so a
// typed value persists without needing Enter.
els.ruleEnabled.addEventListener('change', saveRule);
els.ruleCoolTarget.addEventListener('blur', saveRule);
els.ruleHeatTarget.addEventListener('blur', saveRule);

// Schedules — dynamic list; each row wires its own controls when rendered.
els.schedAdd.addEventListener('click', function () {
  if (!state.selectedId) return;
  const unit = unitById(state.selectedId);
  if (!unit) return;
  currentScheduleEntries.push(scheduleDefaults(unit));
  renderScheduleList(unit);
  saveSchedules();
});

els.loginForm.addEventListener('submit', async function (ev) {
  ev.preventDefault();
  els.loginError.hidden = true;
  const password = els.loginPassword.value;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    const body = await res.json().catch(function () { return null; });
    if (!res.ok || !body || !body.token) {
      els.loginError.textContent = (body && body.detail) || 'Login failed';
      els.loginError.hidden = false;
      return;
    }
    writeToken(body.token);
    hideLogin();
    loadUnits();
  } catch (exc) {
    els.loginError.textContent = String(exc.message || exc);
    els.loginError.hidden = false;
  }
});

(function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  // Tabs: register the energy controller as the tab-change hook, then select
  // the remembered tab — setTab fires onEnergyTab, which also sets the poll
  // cadence (fast on Energy, slow elsewhere) and lazily builds the charts.
  wireTabs();
  wireEnergyControls();
  wirePlugsToggle();
  wirePlugDetail();
  wireZoneDetail();
  wireSecurityHiddenToggle();
  // Energy, Plugs, and Security adjust their own polling cadence on tab change,
  // so fan the single switcher hook out to each controller.
  onTabChange(function (tab) {
    onEnergyTab(tab); onPlugsTab(tab); onSecurityTab(tab);
    // Keep Home clean — Settings lives on the other tabs only (issue #72).
    els.settingsCard.hidden = tab === 'home';
  });
  setTab(initialTab());

  loadUnits();
  loadEnergy();
  startWeatherPolling();
  fetchVersion();
  setInterval(loadUnits, 30_000);
  setInterval(fetchVersion, 300_000);
})();
