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

import { state, els, toast, PLUGS_SHOW_ALL_KEY } from './state.js';
import { jsonApi } from './api.js';

const POLL_MS = 15_000;

let plugsTimer = null;

// --------------------------------------------------------------- formatting
function fmtW(v) {
  return v == null ? '—' : Math.round(Number(v)) + ' W';
}

function deviceById(id) {
  return state.plugs.find(function (d) { return d.device_id === id; });
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
    toast(device.name + ' ' + action, 'good');
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

  // --- Top band: name + power toggle (when switchable & reachable). ---
  const top = document.createElement('div');
  top.className = 'plug-top';

  const name = document.createElement('span');
  name.className = 'plug-name';
  name.textContent = device.name || 'Device';
  top.appendChild(name);

  if (device.has_switch && device.reachable) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle' + (on ? ' on' : '');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', on ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Power ' + (device.name || 'device'));
    toggle.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
      (on ? 'ON' : 'OFF') + '</span>';
    toggle.addEventListener('click', function () { toggleSwitch(device); });
    top.appendChild(toggle);
  }
  card.appendChild(top);

  // --- Live wattage: first-class on metered plugs. ---
  if (device.metered && device.reachable && device.power_w != null) {
    const watts = document.createElement('div');
    watts.className = 'plug-watts';
    watts.textContent = fmtW(device.power_w);
    card.appendChild(watts);
  }

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

export function renderPlugs() {
  els.plugsGrid.innerHTML = '';

  // Update toggle button label to reflect current state.
  if (els.plugsToggleBtn) {
    els.plugsToggleBtn.textContent = state.plugsShowAll ? 'Hide unregistered' : 'Show all';
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
  els.plugsNote.hidden = true;

  const sorted = state.plugs.slice().sort(function (a, b) {
    return (a.name || '').localeCompare(b.name || '');
  });

  // When "show all" is off (default), hide devices without a valid LAN IP.
  // Registered-but-offline devices (has_valid_ip=true, reachable=false) still show.
  const visible = state.plugsShowAll
    ? sorted
    : sorted.filter(function (d) { return d.has_valid_ip === true; });

  const hiddenCount = sorted.length - visible.length;
  if (els.plugsHiddenCount) {
    if (!state.plugsShowAll && hiddenCount > 0) {
      els.plugsHiddenCount.textContent =
        hiddenCount + ' unregistered hidden';
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

export async function loadPlugs() {
  try {
    const body = await jsonApi('/api/tuya');
    state.plugs = (body && body.devices) || [];
    renderPlugs();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    // Missing devices.json (503) or a read error — guide, don't crash.
    els.plugsGrid.innerHTML = '';
    els.plugsNote.hidden = false;
    els.plugsNote.textContent = exc.message || 'Failed to load devices.';
  }
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
  } else {
    schedule(0);
  }
}
