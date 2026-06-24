/* Smart Life (Plugs) data + tab controller.
 *
 * Owns the local Tuya device grid: on/off switches, live wattage on metered
 * plugs, and open/close/stop controls for covers. All cloud-free — it reads
 * GET /api/tuya (which does per-device LAN reads) and writes the switch/cover
 * endpoints, updating just the touched card from the read-back.
 *
 * Cadence is tab-aware like energy.js: it polls only while the Plugs tab is
 * open (LAN reads are comparatively expensive) and stops on leave. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk, PLUGS_SHOW_ALL_KEY } from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';

const POLL_MS = 15_000;

let plugsTimer = null;

// --------------------------------------------------------------- formatting
function fmtW(v) {
  return v == null ? '—' : Math.round(Number(v)) + ' W';
}

function deviceById(id) {
  return state.plugs.find(function (d) { return d.device_id === id; });
}

// Custom override (PUT /api/tuya/{id}/display_name) wins over the Tuya name.
function plugLabel(device) {
  return device.display_name || device.name || '';
}

// ----------------------------------------------------------------- write
async function toggleSwitch(device) {
  const next = !(device.switch_on === true);
  try {
    const updated = await jsonApi(
      '/api/tuya/' + encodeURIComponent(device.device_id) + '/switch',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: next }),
      },
    );
    state.plugs = state.plugs.map(function (d) {
      return d.device_id === device.device_id ? Object.assign({}, d, updated) : d;
    });
    renderPlugs();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function coverAction(device, action) {
  try {
    await jsonApi(
      '/api/tuya/' + encodeURIComponent(device.device_id) + '/cover',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action }),
      },
    );
    toast(plugLabel(device) + ' ' + action, 'good');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

// ------------------------------------------------------------- card DOM
function buildCard(device) {
  const on = device.switch_on === true;
  const card = document.createElement('article');
  card.className = 'card plug-card';
  card.dataset.deviceId = device.device_id;
  if (!device.reachable) card.classList.add('is-unavailable');
  else if (device.has_switch && !on) card.classList.add('is-off');

  // --- Top band: one row — name (tap to rename) · wattage · toggle. The
  //     wattage sits immediately left of the toggle so metered and unmetered
  //     tiles share the same row shape (and thus the same height). ---
  const top = document.createElement('div');
  top.className = 'plug-top';

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'plug-name';
  name.title = 'Rename';
  name.textContent = plugLabel(device) || 'Device';
  name.addEventListener('click', function () { openPlugDetail(device.device_id); });
  top.appendChild(name);

  // Live wattage on metered, reachable plugs — just left of the toggle.
  if (device.metered && device.reachable && device.power_w != null) {
    const watts = document.createElement('span');
    watts.className = 'plug-watts';
    watts.textContent = fmtW(device.power_w);
    top.appendChild(watts);
  }

  if (device.has_switch && device.reachable) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle' + (on ? ' on' : '');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Power ' + (plugLabel(device) || 'device'));
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (on ? 'ON' : 'OFF') + '</span>';
    toggle.addEventListener('click', function () { toggleSwitch(device); });
    top.appendChild(toggle);
  }
  card.appendChild(top);

  // --- Cover controls (open / stop / close). ---
  if (device.has_cover && device.reachable) {
    const cover = document.createElement('div');
    cover.className = 'plug-cover';
    [['open', '▲ Open'], ['stop', '■ Stop'], ['close', '▼ Close']].forEach(function (pair) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cover-btn';
      btn.dataset.action = pair[0];
      btn.textContent = pair[1];
      btn.addEventListener('click', function () { coverAction(device, pair[0]); });
      cover.appendChild(btn);
    });
    card.appendChild(cover);
  }

  // --- Unavailable note (offline / no local IP). ---
  if (!device.reachable) {
    const note = document.createElement('div');
    note.className = 'plug-unavailable';
    note.textContent = device.error || 'Unavailable';
    card.appendChild(note);
  }

  return card;
}

// --------------------------------------------------------- rename modal
function openPlugDetail(deviceId) {
  const device = deviceById(deviceId);
  if (!device) return;
  state.selectedPlugId = deviceId;
  els.plugDetailName.textContent = plugLabel(device) || 'Device';
  els.plugDisplayName.value = device.display_name || '';
  els.plugDisplayName.placeholder = device.name || 'Custom label…';
  if (typeof els.plugDialog.showModal === 'function') els.plugDialog.showModal();
  else els.plugDialog.setAttribute('open', '');
  els.plugDisplayName.focus();
}

function closePlugDetail() {
  state.selectedPlugId = null;
  if (typeof els.plugDialog.close === 'function') els.plugDialog.close();
  else els.plugDialog.removeAttribute('open');
}

async function savePlugName() {
  if (!state.selectedPlugId) return;
  const id = state.selectedPlugId;
  const newName = els.plugDisplayName.value.trim();
  try {
    await jsonApi('/api/tuya/' + encodeURIComponent(id) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    state.plugs = state.plugs.map(function (d) {
      return d.device_id === id ? Object.assign({}, d, { display_name: newName || null }) : d;
    });
    const device = deviceById(id);
    if (device) els.plugDetailName.textContent = plugLabel(device) || 'Device';
    renderPlugs();
    toast('Name saved', 'good');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

// ----------------------------------------------------------- summary stats
// Totals over every known device (state.plugs), independent of the show-all
// filter: devices, switches on, switches off, and live watts on reachable
// metered plugs.
function renderStats() {
  // The same totals render in the Plugs tab card and the Home tab tile (#72).
  const cards = [els.plugsStats, els.homePlugsStats];
  if (!state.plugs.length) {
    cards.forEach(function (c) { if (c) c.hidden = true; });
    return;
  }
  let on = 0;
  let off = 0;
  let watts = 0;
  state.plugs.forEach(function (d) {
    if (d.switch_on === true) on += 1;
    else if (d.has_switch && d.switch_on === false) off += 1;
    if (d.metered && d.reachable && d.power_w != null) watts += Number(d.power_w);
  });
  const total = String(state.plugs.length);
  const onStr = String(on);
  const offStr = String(off);
  const wattStr = fmtW(watts);
  const set = function (el, v) { if (el) el.textContent = v; };
  set(els.plugStatTotal, total); set(els.homePlugStatTotal, total);
  set(els.plugStatOn, onStr); set(els.homePlugStatOn, onStr);
  set(els.plugStatOff, offStr); set(els.homePlugStatOff, offStr);
  set(els.plugStatWatts, wattStr); set(els.homePlugStatWatts, wattStr);
  cards.forEach(function (c) { if (c) c.hidden = false; });
}

export function renderPlugs() {
  els.plugsGrid.innerHTML = '';
  renderStats();

  // Update toggle button label to reflect current state.
  if (els.plugsToggleBtn) {
    els.plugsToggleBtn.textContent = state.plugsShowAll ? 'Reachable only' : 'Show all devices';
    els.plugsToggleBtn.classList.toggle('active', state.plugsShowAll);
  }

  if (!state.plugs.length) {
    els.plugsNote.hidden = false;
    els.plugsNote.textContent =
      'No Smart Life devices. Refresh devices.json on the home network ' +
      '(python -m tinytuya wizard) to capture them.';
    if (els.plugsHiddenCount) els.plugsHiddenCount.hidden = true;
    return;
  }
  if (isSnapshotRestored('plugs')) {
    els.plugsNote.hidden = false;
    els.plugsNote.textContent = snapshotLabel('plugs');
  } else {
    els.plugsNote.hidden = true;
  }

  const sorted = state.plugs.slice().sort(function (a, b) {
    return (a.name || '').localeCompare(b.name || '');
  });

  // When "show all" is off, hide devices without a valid LAN IP.
  // Registered-but-offline devices (has_valid_ip=true, reachable=false) still show.
  const visible = state.plugsShowAll
    ? sorted
    : sorted.filter(function (d) { return d.has_valid_ip === true; });

  const hiddenCount = sorted.length - visible.length;
  if (els.plugsHiddenCount) {
    if (!state.plugsShowAll && hiddenCount > 0) {
      els.plugsHiddenCount.textContent = hiddenCount + ' no-IP hidden';
      els.plugsHiddenCount.hidden = false;
    } else {
      els.plugsHiddenCount.hidden = true;
    }
  }

  visible.forEach(function (d) { els.plugsGrid.appendChild(buildCard(d)); });
}

// ------------------------------------------------------- toggle wiring
export function wirePlugsToggle() {
  // Restore persisted preference on page load.
  try {
    const stored = localStorage.getItem(PLUGS_SHOW_ALL_KEY);
    if (stored === 'true') state.plugsShowAll = true;
    else if (stored === 'false') state.plugsShowAll = false;
  } catch (_) { /* private mode */ }

  if (!els.plugsToggleBtn) return;
  els.plugsToggleBtn.addEventListener('click', function () {
    state.plugsShowAll = !state.plugsShowAll;
    try {
      localStorage.setItem(PLUGS_SHOW_ALL_KEY, String(state.plugsShowAll));
    } catch (_) { /* private mode */ }
    renderPlugs();
  });
}

export function wirePlugsRefresh() {
  if (!els.plugsRefresh) return;
  els.plugsRefresh.addEventListener('click', async function () {
    els.plugsRefresh.disabled = true;
    try {
      const body = await jsonApi('/api/tuya/refresh', { method: 'POST' });
      reportFetchOk('plugs');
      saveSnapshot('plugs', body);
      state.plugs = (body && body.devices) || [];
      renderPlugs();
      toast('Plugs refreshed', 'good');
    } catch (exc) {
      if (String(exc.message) !== 'auth required') {
        reportFetchFailure('plugs', exc, 'plugs');
        els.plugsNote.hidden = false;
        els.plugsNote.textContent = exc.message || 'Failed to refresh devices.';
      }
    } finally {
      els.plugsRefresh.disabled = false;
    }
  });
}

// Wire the rename modal once at boot (mirrors the AC detail-modal wiring).
export function wirePlugDetail() {
  els.plugDetailClose.addEventListener('click', closePlugDetail);
  els.plugDialog.addEventListener('click', function (ev) {
    if (ev.target === els.plugDialog) closePlugDetail();  // backdrop click
  });
  els.plugDisplayName.addEventListener('blur', savePlugName);
  els.plugDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.plugDisplayName.blur(); }
  });
}

export async function loadPlugs() {
  try {
    const body = await jsonApi('/api/tuya');
    reportFetchOk('plugs');
    saveSnapshot('plugs', body);
    state.plugs = (body && body.devices) || [];
    renderPlugs();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    // Missing devices.json (503) or a read error — guide, don't crash. The
    // inline note stays for context; the toast surfaces the reason once.
    reportFetchFailure('plugs', exc, 'plugs');
    if (!state.plugs.length) els.plugsGrid.innerHTML = '';
    els.plugsNote.hidden = false;
    els.plugsNote.textContent = exc.message || 'Failed to load devices.';
  }
}

export function restorePlugsSnapshot() {
  const body = restoreSnapshot('plugs');
  if (!body) return;
  state.plugs = (body && body.devices) || [];
  renderPlugs();
}

// --------------------------------------------------------- cadence + tabs
function schedule(ms) {
  if (plugsTimer) clearInterval(plugsTimer);
  plugsTimer = ms > 0 ? setInterval(loadPlugs, ms) : null;
}

// Called by the tab switcher whenever the active tab changes. LAN reads are
// expensive, so only poll while the Plugs tab is open; stop when it isn't.
export function onPlugsTab(tab) {
  if (tab === 'plugs') {
    loadPlugs();            // immediate refresh on entry (also the first load)
    schedule(POLL_MS);
  } else if (tab === 'home') {
    // Home shows the (informative) plug summary: load once on entry, but do not
    // start the comparatively expensive LAN polling on the default tab (#72).
    loadPlugs();
    schedule(0);
  } else {
    schedule(0);
  }
}
