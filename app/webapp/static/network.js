/* Network (LAN) tab controller.
 *
 * Owns the home-network view: internet health (+ opt-in speed test), AP/router
 * health with the confirm-gated AP reboot, and the attached-device inventory
 * grouped by band (weakest signal first). Read-mostly — it reads GET /api/network
 * and writes only the one POST /api/network/access-point/reboot.
 *
 * Cadence is tab-aware like plugs.js/energy.js: the AP SOAP read is comparatively
 * expensive, so it polls only while the Network tab is open and stops on leave.
 * The speed test never auto-runs — it is an explicit button that adds ~13 s.
 */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';

const POLL_MS = 15_000;
// Mirrors src.network_client._WEAK_SIGNAL_PCT — a wireless client below this is
// counted in the "Weak" chip and dimmed in the list.
const WEAK_SIGNAL_PCT = 40;
// Device-list group order + display labels (wireless bands first, then wired).
const GROUPS = [
  { key: '5GHz', label: '5 GHz' },
  { key: '2.4GHz', label: '2.4 GHz' },
  { key: 'wired', label: 'Wired' },
];

let networkTimer = null;
let speedtestRunning = false;
// Last successful speed-test result, kept across polls (a normal poll returns
// null Mbps, which would otherwise wipe the displayed figure each cycle).
let lastSpeed = null;

// --------------------------------------------------------------- formatting
function fmtMs(v) { return v == null ? '—' : Math.round(Number(v)) + ' ms'; }
function fmtPct(v) { return v == null ? '—' : Math.round(Number(v)) + '%'; }
function fmtMbps(v) { return v == null ? '—' : Math.round(Number(v)) + ' Mbps'; }

// --------------------------------------------------- reusable confirm dialog
// A styled <dialog> confirm (issue #129) returning a Promise<boolean>. Exported
// so later writes (e.g. the Phase-3 router reboot) reuse it rather than a native
// confirm() — the one design-system-breaking element this app would otherwise have.
let confirmResolver = null;

function closeConfirm(result) {
  if (typeof els.confirmDialog.close === 'function') els.confirmDialog.close();
  else els.confirmDialog.removeAttribute('open');
  const resolve = confirmResolver;
  confirmResolver = null;
  if (resolve) resolve(result);
}

export function confirmAction(opts) {
  els.confirmTitle.textContent = (opts && opts.title) || 'Confirm';
  els.confirmMessage.textContent = (opts && opts.message) || '';
  els.confirmOk.textContent = (opts && opts.okLabel) || 'Confirm';
  els.confirmOk.classList.toggle('is-danger', !!(opts && opts.danger));
  return new Promise(function (resolve) {
    confirmResolver = resolve;
    if (typeof els.confirmDialog.showModal === 'function') els.confirmDialog.showModal();
    else els.confirmDialog.setAttribute('open', '');
  });
}

export function wireConfirmDialog() {
  if (!els.confirmDialog) return;
  els.confirmClose.addEventListener('click', function () { closeConfirm(false); });
  els.confirmCancel.addEventListener('click', function () { closeConfirm(false); });
  els.confirmOk.addEventListener('click', function () { closeConfirm(true); });
  els.confirmDialog.addEventListener('click', function (ev) {
    if (ev.target === els.confirmDialog) closeConfirm(false);  // backdrop click
  });
  // Esc fires the native 'cancel' event — resolve false and let it close.
  els.confirmDialog.addEventListener('cancel', function () { closeConfirm(false); });
}

// ----------------------------------------------------------------- render
function renderInternet(net) {
  const online = !!(net && net.online);
  els.netInternetStatus.textContent = net ? (online ? 'Online' : 'Offline') : '—';
  els.netInternetStatus.className = 'net-internet-status ' +
    (net ? (online ? 'is-online' : 'is-offline') : '');

  if (!net) {
    els.netInternetMeta.textContent = '—';
    return;
  }
  const parts = [];
  if (net.external_ms != null) parts.push(fmtMs(net.external_ms) + ' latency');
  if (net.packet_loss_pct != null) parts.push(fmtPct(net.packet_loss_pct) + ' loss');
  if (net.gateway_ms != null) parts.push('gateway ' + fmtMs(net.gateway_ms));
  els.netInternetMeta.textContent = parts.length ? parts.join(' · ') : 'No reply from the outside world.';

  // A fresh speed test (Mbps present) becomes the sticky "last result"; normal
  // polls leave lastSpeed untouched so the figure persists between cycles.
  if (net.download_mbps != null || net.upload_mbps != null) {
    lastSpeed = {
      down: net.download_mbps,
      up: net.upload_mbps,
      server: net.speedtest_server || null,
    };
  }
  if (lastSpeed) {
    const seg = ['↓ ' + fmtMbps(lastSpeed.down), '↑ ' + fmtMbps(lastSpeed.up)];
    if (lastSpeed.server) seg.push('via ' + lastSpeed.server);
    els.netSpeedResult.textContent = seg.join(' · ');
    els.netSpeedResult.hidden = false;
  } else {
    els.netSpeedResult.hidden = true;
  }
}

function renderAlerts(alerts) {
  const list = alerts || [];
  els.netAlerts.innerHTML = '';
  if (!list.length) {
    els.netAlerts.hidden = true;
    return;
  }
  els.netAlerts.hidden = false;
  list.forEach(function (text) {
    const row = document.createElement('div');
    row.className = 'net-alert';
    row.textContent = '⚠ ' + text;
    els.netAlerts.appendChild(row);
  });
}

function renderHealth(ap, router) {
  // Access point.
  els.netApName.textContent = (ap && ap.model) || 'Access point';
  if (ap && ap.reachable) {
    const bits = [];
    if (ap.mode) bits.push(ap.mode.replace('_', ' '));
    if (ap.firmware) bits.push('FW ' + ap.firmware);
    bits.push(ap.device_count + (ap.device_count === 1 ? ' device' : ' devices'));
    els.netApMeta.textContent = bits.join(' · ');
    els.netApMeta.classList.remove('is-error');
    els.netApReboot.hidden = false;
  } else {
    els.netApMeta.textContent = 'Unreachable' + (ap && ap.error ? ': ' + ap.error : '');
    els.netApMeta.classList.add('is-error');
    els.netApReboot.hidden = true;  // can't reboot what we can't reach
  }

  // Router — reachability + login signal only (WAN detail is Phase 3).
  els.netRouterName.textContent = (router && router.model) || 'Router';
  if (router && router.reachable) {
    els.netRouterMeta.textContent = 'Reachable · ' +
      (router.authenticated ? 'login OK' : 'login failed');
    els.netRouterMeta.classList.toggle('is-error', !router.authenticated);
  } else {
    els.netRouterMeta.textContent = 'Unreachable' + (router && router.error ? ': ' + router.error : '');
    els.netRouterMeta.classList.add('is-error');
  }
}

function renderStats(devices) {
  const list = devices || [];
  if (!list.length) {
    els.netStats.hidden = true;
    return;
  }
  const counts = { wired: 0, '5GHz': 0, '2.4GHz': 0 };
  let weak = 0;
  list.forEach(function (d) {
    if (d.conn_type && counts[d.conn_type] !== undefined) counts[d.conn_type] += 1;
    if (d.is_wireless && d.signal != null && d.signal < WEAK_SIGNAL_PCT) weak += 1;
  });
  const chips = [
    ['Wired', counts.wired],
    ['5GHz', counts['5GHz']],
    ['2.4GHz', counts['2.4GHz']],
    ['Weak', weak],
  ];
  els.netStats.innerHTML = '';
  chips.forEach(function (pair) {
    const chip = document.createElement('span');
    chip.className = 'net-stat-chip' + (pair[0] === 'Weak' && pair[1] > 0 ? ' is-weak' : '');
    chip.innerHTML = '<span class="net-stat-num">' + pair[1] + '</span> ' + pair[0];
    els.netStats.appendChild(chip);
  });
  els.netStats.hidden = false;
}

function deviceLabel(d) {
  return d.name || d.mac || '(unknown)';
}

// Weakest signal first within a group; nulls (e.g. wired) sort last, then by name.
function bySignalThenName(a, b) {
  const sa = a.signal == null ? 1000 : a.signal;
  const sb = b.signal == null ? 1000 : b.signal;
  if (sa !== sb) return sa - sb;
  return deviceLabel(a).localeCompare(deviceLabel(b), undefined, { sensitivity: 'base' });
}

function buildDeviceRow(d) {
  const row = document.createElement('div');
  row.className = 'net-device';
  const weak = d.is_wireless && d.signal != null && d.signal < WEAK_SIGNAL_PCT;
  if (weak) row.classList.add('is-weak');

  const name = document.createElement('span');
  name.className = 'net-device-name';
  name.textContent = deviceLabel(d);
  row.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'net-device-meta';
  const metaBits = [d.ip || '—'];
  if (d.ssid) metaBits.push(d.ssid);
  meta.textContent = metaBits.join(' · ');
  row.appendChild(meta);

  const signal = document.createElement('span');
  signal.className = 'net-device-signal';
  if (d.signal != null) {
    const bar = document.createElement('span');
    bar.className = 'net-signal-bar';
    const fill = document.createElement('span');
    fill.className = 'net-signal-fill';
    fill.style.width = Math.max(0, Math.min(100, d.signal)) + '%';
    bar.appendChild(fill);
    signal.appendChild(bar);
    const pct = document.createElement('span');
    pct.className = 'net-signal-pct';
    pct.textContent = d.signal + '%';
    signal.appendChild(pct);
  } else {
    signal.textContent = d.conn_type === 'wired' ? 'wired' : '—';
  }
  row.appendChild(signal);
  return row;
}

function renderDevices(devices) {
  const list = devices || [];
  els.netDevices.innerHTML = '';
  if (!list.length) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = state.network ? 'No attached devices reported.' : '—';
    return;
  }
  els.netDevicesNote.hidden = true;

  const seen = new Set();
  GROUPS.forEach(function (group) {
    const members = list.filter(function (d) { return d.conn_type === group.key; });
    members.forEach(function (d) { seen.add(d); });
    if (!members.length) return;
    appendGroup(group.label, members.slice().sort(bySignalThenName));
  });
  // Anything with an unknown/missing conn_type lands in a trailing "Other" group.
  const other = list.filter(function (d) { return !seen.has(d); });
  if (other.length) appendGroup('Other', other.slice().sort(bySignalThenName));
}

function appendGroup(label, members) {
  const head = document.createElement('h4');
  head.className = 'net-group-head';
  head.textContent = label + ' · ' + members.length;
  els.netDevices.appendChild(head);
  members.forEach(function (d) { els.netDevices.appendChild(buildDeviceRow(d)); });
}

function renderNetwork() {
  const net = state.network;
  renderInternet(net ? net.internet : null);
  renderAlerts(net ? net.alerts : []);
  renderHealth(net ? net.access_point : null, net ? net.router : null);
  renderStats(net ? net.devices : []);
  renderDevices(net ? net.devices : []);
}

// ----------------------------------------------------------------- load
async function loadNetwork(opts) {
  const speedtest = !!(opts && opts.speedtest);
  try {
    const url = speedtest ? '/api/network?speedtest=1' : '/api/network';
    state.network = await jsonApi(url);
    reportFetchOk('network');
    renderNetwork();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('network', exc, 'network');
    // Keep any last-good render in place; surface the reason in the device note.
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = exc.message || 'Failed to load network.';
  }
}

// ----------------------------------------------------------------- actions
async function runSpeedTest() {
  if (speedtestRunning) return;
  speedtestRunning = true;
  els.netSpeedBtn.disabled = true;
  els.netSpeedBtn.classList.add('is-busy');
  const original = els.netSpeedBtn.innerHTML;
  els.netSpeedBtn.textContent = 'Testing… (~13 s)';
  try {
    await loadNetwork({ speedtest: true });
    toast('Speed test complete', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Speed test failed: ' + (exc.message || exc), 'error');
    }
  } finally {
    speedtestRunning = false;
    els.netSpeedBtn.disabled = false;
    els.netSpeedBtn.classList.remove('is-busy');
    els.netSpeedBtn.innerHTML = original;
  }
}

async function rebootAccessPoint() {
  const ok = await confirmAction({
    title: 'Reboot access point?',
    message: 'All Wi-Fi and wired clients drop for ~1–2 min while the access point restarts.',
    okLabel: 'Reboot',
    danger: true,
  });
  if (!ok) return;
  toast('Rebooting the access point…');
  try {
    await jsonApi('/api/network/access-point/reboot', { method: 'POST' });
    toast('Reboot command accepted — the AP will drop for ~1–2 min', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Reboot failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireNetworkControls() {
  wireConfirmDialog();
  if (els.netSpeedBtn) els.netSpeedBtn.addEventListener('click', runSpeedTest);
  if (els.netApReboot) els.netApReboot.addEventListener('click', rebootAccessPoint);
}

// --------------------------------------------------------- cadence + tabs
function schedule(ms) {
  if (networkTimer) clearInterval(networkTimer);
  networkTimer = ms > 0 ? setInterval(loadNetwork, ms) : null;
}

// The AP SOAP read is expensive, so only poll while the Network tab is open.
export function onNetworkTab(tab) {
  if (tab === 'network') {
    loadNetwork();      // immediate refresh on entry (also the first load)
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
