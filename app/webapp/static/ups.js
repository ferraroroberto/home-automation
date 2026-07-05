/* Local USB UPS tile for Plugs + Home.
 *
 * Reads GET /api/ups. The backend prefers NUT when available and otherwise uses
 * Windows USB-HID battery telemetry, so the connected PC UPS works without
 * vendor cloud software. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { loadPowerNotifyPrefs } from './ups-notify.js';
import { createPoller } from './poll.js';

const POLL_MS = 15_000;

let lastMainsOnline = null;

function fmtPct(v) {
  return v == null ? '—' : Math.round(Number(v)) + '%';
}

function fmtW(v) {
  return v == null ? '—' : Math.round(Number(v)) + ' W';
}

function fmtVolt(v) {
  return v == null ? '—' : Number(v).toFixed(1) + ' V';
}

function fmtRuntime(seconds) {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const mins = Math.round(total / 60);
  if (mins < 60) return mins + ' min';
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h + ' h ' + String(m).padStart(2, '0') + ' min';
}

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function statusText(ups) {
  if (!ups || ups.available !== true) return 'Unavailable';
  if (ups.mains_online === false) return 'On battery';
  if (ups.status === 'charging') return 'Charging';
  if (ups.status === 'full') return 'Full';
  if (ups.status && ups.status.indexOf('low_battery') >= 0) return 'Low battery';
  if (ups.status && ups.status.indexOf('critical') >= 0) return 'Critical';
  return 'Online';
}

function renderStat(label, value) {
  return '<div class="ups-stat ups-stat-' + esc(label.toLowerCase()) + '"><span class="ups-stat-label">' + esc(label) +
    '</span><span class="ups-stat-value">' + esc(value) + '</span></div>';
}

function renderUpsTile(tile, ups, compact) {
  if (!tile) return;
  tile.hidden = false;
  const available = ups && ups.available === true;
  const onBattery = available && ups.mains_online === false;
  const alarms = (ups && ups.alarms) || [];
  tile.classList.toggle('is-on-battery', onBattery);
  tile.classList.toggle('is-unavailable', !available);

  const title = 'UPS';
  const snapshot = isSnapshotRestored('ups') ? '<span class="snapshot-badge">' + esc(snapshotLabel('ups')) + '</span>' : '';
  const identity =
    '<div class="ups-title"><svg class="icon title-icon" aria-hidden="true"><use href="#i-battery-charging"></use></svg><span>' + esc(title) + '</span>' + snapshot + '</div>';

  // Home tile (#253): one line at weather-tile height — identity, then bare
  // charge % and runtime pulled onto the title row (no labels — a % and a
  // duration read for themselves), then the status pill hard-right. The Plugs
  // tab keeps the full stacked stat grid below.
  if (compact) {
    tile.innerHTML =
      '<div class="ups-main">' +
      identity +
      '<span class="ups-line-stats"><span>' + esc(fmtPct(ups && ups.battery_charge_pct)) + '</span>' +
      '<span>' + esc(fmtRuntime(ups && ups.runtime_seconds)) + '</span></span>' +
      '<span class="ups-status">' + esc(statusText(ups)) + '</span>' +
      '</div>';
    return;
  }

  const stats = [
    renderStat('Charge', fmtPct(ups && ups.battery_charge_pct)),
    renderStat('Runtime', fmtRuntime(ups && ups.runtime_seconds)),
    renderStat('Battery', fmtVolt(ups && ups.battery_voltage_v)),
    renderStat('Load', (ups && ups.load_pct != null) ? fmtPct(ups.load_pct) : fmtW(ups && ups.load_w)),
    renderStat('Input', fmtVolt(ups && ups.input_voltage_v)),
  ].join('');
  const alarmHtml = alarms.length
    ? '<div class="ups-alerts">' + alarms.map(function (a) { return '<span>' + esc(a) + '</span>'; }).join('') + '</div>'
    : '';

  tile.innerHTML =
    '<div class="ups-main">' +
    '  <div class="ups-identity">' +
    '    ' + identity +
    '  </div>' +
    '  <div class="ups-stats">' + stats + '</div>' +
    '  <span class="ups-status">' + esc(statusText(ups)) + '</span>' +
    '</div>' +
    alarmHtml;
}

export function renderUps() {
  // Both tiles use the compact one-line layout — the Plugs tile is identical to
  // the Home tile (the container already carries `ups-tile-compact`).
  renderUpsTile(els.upsTile, state.ups, true);
  renderUpsTile(els.homeUpsTile, state.ups, true);
}

function handleTransition(next) {
  if (!next || next.available !== true || next.mains_online == null) return;
  if (lastMainsOnline == null) {
    lastMainsOnline = next.mains_online;
    return;
  }
  if (lastMainsOnline === true && next.mains_online === false) {
    toast('Power outage: PC and Wi-Fi are on UPS battery', 'error');
  } else if (lastMainsOnline === false && next.mains_online === true) {
    toast('Power restored: UPS is back on mains', 'good');
  }
  lastMainsOnline = next.mains_online;
}

export async function loadUps() {
  try {
    const body = await jsonApi('/api/ups');
    reportFetchOk('ups');
    saveSnapshot('ups', body);
    state.ups = (body && body.ups) || null;
    handleTransition(state.ups);
    renderUps();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('ups', exc, 'UPS');
    renderUps();
  }
}

export function restoreUpsSnapshot() {
  const body = restoreSnapshot('ups');
  if (!body) return;
  state.ups = (body && body.ups) || null;
  renderUps();
}

const schedule = createPoller(loadUps);

export function onUpsTab(tab) {
  if (tab === 'plugs' || tab === 'home') {
    loadUps();
    if (tab === 'plugs') loadPowerNotifyPrefs();
    schedule(tab === 'plugs' ? POLL_MS : 0);
  } else {
    schedule(0);
  }
}
