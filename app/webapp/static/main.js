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
import { jsonApi, hideLogin } from './api.js';
import { setTab, wireTabs, onTabChange, initialTab } from './tabs.js';
import {
  loadEnergy,
  wireEnergyControls,
  onEnergyTab,
  restyleEnergyCharts,
} from './energy.js';
import { onPlugsTab, wirePlugsToggle, wirePlugDetail } from './plugs.js';
import { onSecurityTab } from './security.js';
import { startWeatherPolling } from './weather.js';

const DEFAULT_RANGE = [16, 31];

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
  header.innerHTML =
    '<span class="unit-mode-icon">' + modeIcon(unit.operation_mode) + '</span>' +
    '<span class="unit-name"></span>' +
    '<span class="unit-chevron">›</span>';
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
  top.appendChild(power);

  // Fan speed — compact, label-less in the top band (aria-label for a11y).
  if (unit.fan_speeds && unit.fan_speeds.length) {
    const sel = document.createElement('select');
    sel.className = 'select-native unit-fan';
    sel.setAttribute('aria-label', 'Fan speed');
    unit.fan_speeds.forEach(function (f) {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f;
      if (f === unit.fan_speed) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', function () {
      applyControl(unit.unit_id, { fan_speed: sel.value });
    });
    top.appendChild(sel);
  }

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

  els.detailVaneVerticalRow.hidden = !unit.has_vane_vertical;
  if (unit.has_vane_vertical) {
    fillSelect(els.detailVaneVertical, unit.vane_vertical_options || [], unit.vane_vertical);
  }
  els.detailVaneHorizontalRow.hidden = !unit.has_vane_horizontal;
  if (unit.has_vane_horizontal) {
    fillSelect(els.detailVaneHorizontal, unit.vane_horizontal_options || [], unit.vane_horizontal);
  }
}

function openDetail(unitId) {
  const unit = unitById(unitId);
  if (!unit) return;
  state.selectedId = unitId;
  populateDetail(unit);
  if (typeof els.detail.showModal === 'function') els.detail.showModal();
  else els.detail.setAttribute('open', '');
}

function closeDetail() {
  state.selectedId = null;
  if (typeof els.detail.close === 'function') els.detail.close();
  else els.detail.removeAttribute('open');
}

// --------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running — confirms a tray
  // restart actually picked up new code. Uses jsonApi so the bearer token
  // is attached (/api/version is auth-gated like the rest of the API).
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const ts = (body.built_at || '').replace('T', ' ').slice(0, 16);
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
    name.textContent = modeIcon(u.operation_mode) + ' ' + (displayLabel(u) || 'Unit');

    // Centred temperature column: room → target on top, mode · fan beneath, so
    // the readings line up down the card (issue #72).
    const center = document.createElement('span');
    center.className = 'ac-line-center';
    const room = fmtTemp(u.room_temperature);
    const target = fmtTemp(u.set_temperature);
    center.innerHTML =
      '<span class="ac-temp">' + room + ' → ' + target + '</span>' +
      '<span class="ac-meta">' + (u.operation_mode || '—') +
        (u.fan_speed ? ' · ' + u.fan_speed : '') + '</span>';

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

// --------------------------------------------------------------- theme toggle
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const icon = dark ? '☀️' : '🌙';
  // Two toggles share the state: the Settings one (other tabs) and the weather
  // tile one (Home, which has no Settings card) — keep both icons in sync (#72).
  els.themeBtn.textContent = icon;
  if (els.weatherThemeBtn) els.weatherThemeBtn.textContent = icon;
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
els.detailVaneVertical.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { vane_vertical_direction: els.detailVaneVertical.value });
});
els.detailVaneHorizontal.addEventListener('change', function () {
  if (state.selectedId) applyControl(state.selectedId, { vane_horizontal_direction: els.detailVaneHorizontal.value });
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
})();
