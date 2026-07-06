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

import { state, els, toast, reportFetchFailure, reportFetchOk, PLUGS_SHOW_ALL_KEY, PLUGS_SHOW_HIDDEN_KEY } from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { createPoller } from './poll.js';

const POLL_MS = 15_000;

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
// Per-device in-flight guard (issue #368): toggling plug A must not block
// plug B, but a double-tap on the same toggle must not double-POST.
const switchBusy = new Set();

async function toggleSwitch(device, btn) {
  if (switchBusy.has(device.device_id)) return;
  switchBusy.add(device.device_id);
  if (btn) btn.disabled = true;
  const next = !(device.switch_on === true);
  try {
    toast('Sending…', 'pending');
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
    toast(plugLabel(device) + (next ? ' on' : ' off'), 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  } finally {
    switchBusy.delete(device.device_id);
    // On success renderPlugs() rebuilt the row; on error the old node stays,
    // so re-enable it explicitly.
    if (btn) btn.disabled = false;
  }
}

async function coverAction(device, action) {
  try {
    toast('Sending…', 'pending');
    await jsonApi(
      '/api/tuya/' + encodeURIComponent(device.device_id) + '/cover',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action }),
      },
    );
    toast(plugLabel(device) + ' ' + action, 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  }
}

// ------------------------------------------------------------- row DOM
// Plugs and blinds render as compact divider-separated rows (the Network
// "Attached devices" style), not chunky sub-cards. The name is a button that
// opens the rename/detail modal — shared by both row kinds.
function nameButton(device) {
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'device-row-name';
  name.title = 'Rename';
  name.textContent = plugLabel(device) || 'Device';
  name.addEventListener('click', function () { openPlugDetail(device.device_id); });
  return name;
}

// A compact status word keeps the device name readable in the row; the full
// reason (often a long sentence) lives in the hover title rather than crushing
// the name to an ellipsis.
function unavailableNote(device) {
  const note = document.createElement('span');
  note.className = 'device-row-note plug-unavailable';
  note.textContent = device.has_valid_ip === false ? 'No IP' : 'Offline';
  if (device.error) note.title = device.error;
  return note;
}

function buildPlugRow(device) {
  const on = device.switch_on === true;
  const row = document.createElement('div');
  row.className = 'device-row plug-row';
  row.dataset.deviceId = device.device_id;

  row.appendChild(nameButton(device));

  // Offline / no-IP: just the name + the reason, no controls.
  if (!device.reachable) {
    row.classList.add('is-unavailable');
    row.appendChild(unavailableNote(device));
    return row;
  }
  if (device.has_switch && !on) row.classList.add('is-off');

  // Live wattage on metered plugs — sits just left of the toggle.
  if (device.metered && device.power_w != null) {
    const watts = document.createElement('span');
    watts.className = 'plug-watts';
    watts.textContent = fmtW(device.power_w);
    row.appendChild(watts);
  }

  if (device.has_switch) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle' + (on ? ' on' : '');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Power ' + (plugLabel(device) || 'device'));
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (on ? 'ON' : 'OFF') + '</span>';
    toggle.addEventListener('click', function () { toggleSwitch(device, toggle); });
    row.appendChild(toggle);
  }
  return row;
}

// Up · Stop · Down icon buttons. Covers expose only open/stop/close on the LAN
// (no native position), so these are the full control surface.
const BLIND_CONTROLS = [
  ['open', 'Open', 'i-chevron-up'],
  ['stop', 'Stop', 'i-square'],
  ['close', 'Close', 'i-chevron-down'],
];

function buildBlindRow(device) {
  const row = document.createElement('div');
  row.className = 'device-row blind-row';
  row.dataset.deviceId = device.device_id;

  row.appendChild(nameButton(device));

  if (!device.reachable) {
    row.classList.add('is-unavailable');
    row.appendChild(unavailableNote(device));
    return row;
  }

  const controls = document.createElement('div');
  controls.className = 'blind-controls';
  BLIND_CONTROLS.forEach(function (spec) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'blind-btn';
    btn.dataset.action = spec[0];
    btn.title = spec[1];
    btn.setAttribute('aria-label', spec[1] + ' ' + (plugLabel(device) || 'blind'));
    btn.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#' + spec[2] + '"></use></svg>';
    btn.addEventListener('click', function () { coverAction(device, spec[0]); });
    controls.appendChild(btn);
  });
  row.appendChild(controls);
  return row;
}

// Hide a list card entirely when it holds no devices; otherwise show the
// per-card count badge in its summary.
function setListCard(card, countEl, n) {
  if (card) card.hidden = n === 0;
  if (countEl) {
    countEl.textContent = String(n);
    countEl.hidden = n === 0;
  }
}

// --------------------------------------------------------- rename modal
// Detail-modal staging (#203 pattern): the display name and Hidden edits are
// held locally and written only on Save. plugStaged holds the working toggle
// state captured when the modal opens; closing discards it.
let plugStaged = null;

function markPlugDirty() {
  if (els.plugSave) els.plugSave.disabled = false;
}

function clearPlugDirty() {
  if (els.plugSave) els.plugSave.disabled = true;
}

function renderPlugHiddenToggle(hidden) {
  const btn = els.plugHiddenToggle;
  if (!btn) return;
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
    (hidden ? 'ON' : 'OFF') + '</span>';
}

function openPlugDetail(deviceId) {
  const device = deviceById(deviceId);
  if (!device) return;
  state.selectedPlugId = deviceId;
  els.plugDetailName.textContent = plugLabel(device) || 'Device';
  els.plugDisplayName.value = device.display_name || '';
  els.plugDisplayName.placeholder = device.name || 'Custom label…';
  // Original Smart Life name stays visible even with a custom label set, so the
  // device can be matched back to the Smart Life app.
  if (els.plugOriginalName) {
    els.plugOriginalName.textContent = device.name ? 'Original name: ' + device.name : '';
  }
  plugStaged = { hidden: !!device.hidden };
  renderPlugHiddenToggle(plugStaged.hidden);
  clearPlugDirty();
  if (typeof els.plugDialog.showModal === 'function') els.plugDialog.showModal();
  else els.plugDialog.setAttribute('open', '');
  els.plugDisplayName.focus();
}

function closePlugDetail() {
  state.selectedPlugId = null;
  plugStaged = null;
  clearPlugDirty();
  if (typeof els.plugDialog.close === 'function') els.plugDialog.close();
  else els.plugDialog.removeAttribute('open');
}

function togglePlugHidden() {
  if (!plugStaged) return;
  plugStaged.hidden = !plugStaged.hidden;
  renderPlugHiddenToggle(plugStaged.hidden);
  markPlugDirty();
}

// Commit the staged edits; only fields that actually changed are sent.
async function savePlugDetail() {
  const id = state.selectedPlugId;
  if (!id || !plugStaged) return;
  const device = deviceById(id);
  if (!device) return;
  if (els.plugSave) els.plugSave.disabled = true;
  const newName = els.plugDisplayName.value.trim();
  const ops = [];
  if ((device.display_name || '') !== newName) {
    ops.push(jsonApi('/api/tuya/' + encodeURIComponent(id) + '/display_name', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    }).then(function () { patchPlug(id, { display_name: newName || null }); }));
  }
  if (!!device.hidden !== plugStaged.hidden) {
    ops.push(jsonApi('/api/tuya/' + encodeURIComponent(id) + '/hidden', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden: plugStaged.hidden }),
    }).then(function () { patchPlug(id, { hidden: plugStaged.hidden }); }));
  }
  try {
    await Promise.all(ops);
    const upd = deviceById(id);
    if (upd) els.plugDetailName.textContent = plugLabel(upd) || 'Device';
    renderPlugs();
    clearPlugDirty();
    toast('Saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save: ' + (exc.message || exc), 'error');
    }
    if (els.plugSave) els.plugSave.disabled = false;
  }
}

function patchPlug(id, patch) {
  state.plugs = state.plugs.map(function (d) {
    return d.device_id === id ? Object.assign({}, d, patch) : d;
  });
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

// The "Show hidden" toggle carries the count and only appears when at least one
// device is user-hidden — same pattern as the Network attached-device list.
function renderHiddenToggle() {
  const btn = els.plugsHiddenToggle;
  if (!btn) return;
  const n = state.plugsUserHiddenCount || 0;
  btn.hidden = n === 0;
  btn.textContent = state.plugsShowHidden ? 'Hide hidden' : 'Show hidden (' + n + ')';
  btn.classList.toggle('active', state.plugsShowHidden);
  btn.setAttribute('aria-pressed', state.plugsShowHidden ? 'true' : 'false');
}

export function renderPlugs() {
  els.plugsList.innerHTML = '';
  els.blindsList.innerHTML = '';
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
    state.plugsUserHiddenCount = 0;
    renderHiddenToggle();
    setListCard(els.plugsCard, els.plugsCount, 0);
    setListCard(els.blindsCard, els.blindsCount, 0);
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

  // User-hidden devices (the per-device Hidden toggle) drop out of both lists
  // unless "Show hidden" is on; the toggle carries the count and only appears
  // when something is hidden (mirrors the Network attached-device list).
  state.plugsUserHiddenCount = visible.filter(function (d) { return !!d.hidden; }).length;
  const shown = state.plugsShowHidden
    ? visible
    : visible.filter(function (d) { return !d.hidden; });
  renderHiddenToggle();

  // Split: covers → Blinds card, everything else → Plugs card.
  const plugs = shown.filter(function (d) { return d.has_cover !== true; });
  const blinds = shown.filter(function (d) { return d.has_cover === true; });
  plugs.forEach(function (d) { els.plugsList.appendChild(buildPlugRow(d)); });
  blinds.forEach(function (d) { els.blindsList.appendChild(buildBlindRow(d)); });
  setListCard(els.plugsCard, els.plugsCount, plugs.length);
  setListCard(els.blindsCard, els.blindsCount, blinds.length);
}

// ------------------------------------------------------- toggle wiring
export function wirePlugsToggle() {
  // Restore persisted preferences on page load.
  try {
    const stored = localStorage.getItem(PLUGS_SHOW_ALL_KEY);
    if (stored === 'true') state.plugsShowAll = true;
    else if (stored === 'false') state.plugsShowAll = false;
    state.plugsShowHidden = localStorage.getItem(PLUGS_SHOW_HIDDEN_KEY) === 'true';
  } catch (_) { /* private mode */ }

  if (els.plugsToggleBtn) {
    els.plugsToggleBtn.addEventListener('click', function () {
      state.plugsShowAll = !state.plugsShowAll;
      try {
        localStorage.setItem(PLUGS_SHOW_ALL_KEY, String(state.plugsShowAll));
      } catch (_) { /* private mode */ }
      renderPlugs();
    });
  }

  if (els.plugsHiddenToggle) {
    els.plugsHiddenToggle.addEventListener('click', function () {
      state.plugsShowHidden = !state.plugsShowHidden;
      try {
        localStorage.setItem(PLUGS_SHOW_HIDDEN_KEY, String(state.plugsShowHidden));
      } catch (_) { /* private mode */ }
      renderPlugs();
    });
  }
}

export function wirePlugsRefresh() {
  if (!els.plugsRefresh) return;
  els.plugsRefresh.addEventListener('click', async function () {
    // A refresh runs a LAN broadcast scan server-side (~8s), so signal that the
    // wait is expected rather than a hang.
    els.plugsRefresh.disabled = true;
    els.plugsRefresh.textContent = 'Scanning…';
    try {
      const body = await jsonApi('/api/tuya/refresh', { method: 'POST' });
      reportFetchOk('plugs');
      saveSnapshot('plugs', body);
      state.plugs = (body && body.devices) || [];
      renderPlugs();
      const info = (body && body.refresh) || {};
      const recovered = (info.updated && info.updated.length) || 0;
      toast(info.detail || 'Plugs refreshed', recovered ? 'success' : '');
    } catch (exc) {
      if (String(exc.message) !== 'auth required') {
        reportFetchFailure('plugs', exc, 'plugs');
        els.plugsNote.hidden = false;
        els.plugsNote.textContent = exc.message || 'Failed to refresh devices.';
      }
    } finally {
      els.plugsRefresh.disabled = false;
      els.plugsRefresh.textContent = 'Refresh';
    }
  });
}

// Wire the rename modal once at boot (mirrors the AC detail-modal wiring).
export function wirePlugDetail() {
  els.plugDetailClose.addEventListener('click', closePlugDetail);
  els.plugDialog.addEventListener('click', function (ev) {
    if (ev.target === els.plugDialog) closePlugDetail();  // backdrop click
  });
  // #203: the name + Hidden edits commit on Save, not on blur/toggle.
  els.plugDisplayName.addEventListener('input', markPlugDirty);
  els.plugDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); savePlugDetail(); }
  });
  if (els.plugHiddenToggle) els.plugHiddenToggle.addEventListener('click', togglePlugHidden);
  if (els.plugSave) els.plugSave.addEventListener('click', savePlugDetail);
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
    if (!state.plugs.length) {
      els.plugsList.innerHTML = '';
      els.blindsList.innerHTML = '';
    }
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
const schedule = createPoller(loadPlugs);

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
