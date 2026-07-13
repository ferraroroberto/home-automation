/* Network (LAN) tab controller — boot/core.
 *
 * Owns the home-network view's top-level shell: internet health (+ opt-in speed
 * test), AP/router health with the confirm-gated reboots, the reusable confirm
 * dialog, the tab poll lifecycle, and the renderNetwork orchestrator. The three
 * feature panels live in sibling modules and are wired in here (issue #197):
 *   ./network-devices.js — attached-device inventory + detail/rename modal
 *   ./network-wifi.js    — Wi-Fi diagnostics + channel charts + Wi-Fi modal
 *   ./network-dhcp.js    — DHCP reservation planner + apply flow
 * Read-mostly — it reads GET /api/network and writes only the AP/router reboots.
 *
 * Cadence is tab-aware like plugs.js/energy.js: the AP SOAP read is comparatively
 * expensive, so it polls only while the Network tab is open and stops on leave.
 * The speed test never auto-runs — it is an explicit button that adds ~13 s.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
} from './state.js';
import { jsonApi } from './api.js';
import { fmtPct } from './format.js';
import {
  isSnapshotRestored,
  restoreSnapshot,
  saveSnapshot,
  snapshotLabel,
} from './snapshots.js';
import { emptyStateEl } from './empty-state.js';
import { restyleWifiChannelChart } from './charts.js';
import { createPoller } from './poll.js';
import {
  renderStats,
  renderDevices,
  wireNetDeviceDetail,
  toggleShowOffline,
  toggleShowHiddenDevices,
  setDeviceSort,
  initShowOfflinePref,
  initShowHiddenDevicesPref,
  initDeviceSortPref,
} from './network-devices.js';
import {
  renderWifi,
  wireNetWifiDetail,
  toggleShowHiddenWifi,
  initShowHiddenWifiPref,
} from './network-wifi.js';
import { wireDhcpPlan } from './network-dhcp.js';

const POLL_MS = 15_000;

let speedtestRunning = false;
let networkLoading = false;
let networkViewState = 'idle';
let networkUpdatedAt = null;
let networkLiveUnavailable = false;
// Last successful speed-test result, kept across polls (a normal poll returns
// null Mbps, which would otherwise wipe the displayed figure each cycle).
let lastSpeed = null;

// --------------------------------------------------------------- formatting
function fmtMs(v) { return v == null ? '—' : Math.round(Number(v)) + ' ms'; }
function fmtMbps(v) { return v == null ? '—' : Math.round(Number(v)) + ' Mbps'; }
function fmtUptime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

function setNetworkViewState(next, opts) {
  networkViewState = next;
  if (opts && opts.updatedAt) networkUpdatedAt = opts.updatedAt;
  if (opts && Object.prototype.hasOwnProperty.call(opts, 'liveUnavailable')) {
    networkLiveUnavailable = opts.liveUnavailable;
  }
}

function lastUpdatedLabel() {
  const raw = networkUpdatedAt || state.snapshotUpdatedAt.network;
  const updated = raw instanceof Date ? raw : new Date(raw || '');
  if (Number.isNaN(updated.getTime())) return 'Last updated earlier';
  return 'Last updated ' + updated.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function renderNetworkFeedback() {
  if (!els.paneNetwork || !els.netFeedback) return;
  els.paneNetwork.dataset.state = networkViewState;
  els.netFeedback.innerHTML = '';
  els.netFeedback.hidden = false;

  if (networkViewState === 'loading') {
    els.netFeedback.appendChild(emptyStateEl('wifi', 'Reading network status…'));
    return;
  }
  if (networkViewState === 'error') {
    els.netFeedback.appendChild(emptyStateEl('wifi', 'Network unavailable', {
      actionLabel: 'Retry',
      onAction: function () { loadNetwork(); },
    }));
    return;
  }
  if (networkViewState === 'stale') {
    const note = document.createElement('p');
    note.className = 'muted small network-stale-note';
    note.textContent = networkLiveUnavailable
      ? lastUpdatedLabel() + ' · live data unavailable'
      : snapshotLabel('network');
    els.netFeedback.appendChild(note);
    return;
  }
  els.netFeedback.hidden = true;
}

function disableStaleNetworkActions() {
  [els.netApReboot, els.netRouterReboot].forEach(function (button) {
    if (button) button.disabled = true;
  });
}

function markNetworkFailure() {
  setNetworkViewState(state.network ? 'stale' : 'error', {
    liveUnavailable: true,
  });
  reportFetchFailure(
    'network',
    { message: 'live data unavailable' },
    'network'
  );
  renderNetworkFeedback();
  if (state.network) disableStaleNetworkActions();
}

// --------------------------------------------------- reusable confirm dialog
// A styled <dialog> confirm (issue #129) returning a Promise<boolean>. Exported
// so later writes (e.g. the Phase-3 router reboot, the DHCP apply) reuse it rather
// than a native confirm() — the one design-system-breaking element this app would
// otherwise have.
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
  els.netInternetStatus.textContent = net ? (online ? 'Online' : 'Offline') : '— no data';
  els.netInternetStatus.className = 'net-internet-status ' +
    (net ? (online ? 'is-online' : 'is-offline') : '');

  if (!net) {
    els.netInternetMeta.textContent = '— no data';
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

function renderAlerts(_alerts) {
  els.netAlerts.innerHTML = '';
  els.netAlerts.hidden = true;
}

function setMetaLines(el, lines) {
  el.innerHTML = '';
  lines.forEach(function (text) {
    const line = document.createElement('span');
    line.className = 'net-health-meta-line';
    line.textContent = text;
    el.appendChild(line);
  });
}

function renderHealth(ap, router) {
  // Access point.
  els.netApName.textContent = (ap && ap.model) || 'Access point';
  if (ap && ap.reachable) {
    const top = [];
    const bottom = [];
    if (ap.mode) top.push(ap.mode.replace('_', ' '));
    if (ap.firmware) bottom.push('FW ' + ap.firmware);
    bottom.push(ap.device_count + (ap.device_count === 1 ? ' device' : ' devices'));
    setMetaLines(els.netApMeta, [top.join(' · ') || 'Access point', bottom.join(' · ')]);
    els.netApMeta.classList.remove('is-error');
    els.netApReboot.hidden = false;
  } else {
    els.netApMeta.textContent = 'Unreachable' + (ap && ap.error ? ': ' + ap.error : '');
    els.netApMeta.classList.add('is-error');
    els.netApReboot.hidden = true;  // can't reboot what we can't reach
  }

  // Router — reachability, login, and the Phase-3 WAN/internet status.
  els.netRouterName.textContent = (router && router.model) || 'Router';
  const routerReachable = !!(router && router.reachable);
  const routerAuthed = !!(router && router.authenticated);
  if (!routerReachable) {
    els.netRouterMeta.textContent = 'Unreachable' + (router && router.error ? ': ' + router.error : '');
    els.netRouterMeta.classList.add('is-error');
  } else if (!routerAuthed) {
    els.netRouterMeta.textContent = 'Reachable · login failed';
    els.netRouterMeta.classList.add('is-error');
  } else if (router.wan_online === true) {
    const top = ['WAN up'];
    if (router.public_ip) top.push(router.public_ip);
    const bottom = router.uptime_s != null ? 'up ' + fmtUptime(router.uptime_s) : 'login OK';
    setMetaLines(els.netRouterMeta, [top.join(' · '), bottom]);
    els.netRouterMeta.classList.remove('is-error');
  } else if (router.wan_online === false) {
    els.netRouterMeta.textContent = 'WAN down · login OK';
    els.netRouterMeta.classList.add('is-error');
  } else {
    // Authenticated but WAN read unavailable (rare): keep the login signal.
    els.netRouterMeta.textContent = 'Reachable · login OK';
    els.netRouterMeta.classList.remove('is-error');
  }
  // Reboot is only possible once we can authenticate to the router.
  if (els.netRouterReboot) {
    els.netRouterReboot.disabled = !routerAuthed;
    els.netRouterReboot.title = routerAuthed
      ? 'Reboot the router (drops the internet ~5 min)'
      : 'Router login required to reboot';
  }
}

function renderNetwork() {
  const net = state.network;
  renderInternet(net ? net.internet : null);
  renderAlerts(net ? net.alerts : []);
  renderHealth(net ? net.access_point : null, net ? net.router : null);
  renderWifi(net ? net.wifi : null);
  renderStats(net
    ? (net.devices || []).filter(function (d) { return state.networkShowHiddenDevices || !d.hidden; })
    : []);
  renderDevices(net ? net.devices : []);
  renderNetworkFeedback();
  if (networkViewState === 'stale' && networkLiveUnavailable) {
    disableStaleNetworkActions();
  }
}

// renderNetwork is the orchestrator the sub-modules call back into after a
// mutation; export it so network-devices.js / network-wifi.js can re-render.
export { renderNetwork };

// ----------------------------------------------------------------- load
async function loadNetwork(opts) {
  const speedtest = !!(opts && opts.speedtest);
  if (networkLoading) return false;
  networkLoading = true;
  if (!speedtest && !state.network) {
    setNetworkViewState('loading', { liveUnavailable: false });
    renderNetworkFeedback();
  }
  try {
    const url = speedtest ? '/api/network?speedtest=1' : '/api/network';
    state.network = await jsonApi(url);
    reportFetchOk('network');
    if (!speedtest) saveSnapshot('network', state.network);
    setNetworkViewState('ready', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderNetwork();
    return true;
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    markNetworkFailure();
    return false;
  } finally {
    networkLoading = false;
  }
}

export function restoreNetworkSnapshot() {
  const body = restoreSnapshot('network');
  if (!body) return;
  state.network = body;
  setNetworkViewState('stale', {
    updatedAt: state.snapshotUpdatedAt.network,
    liveUnavailable: false,
  });
  renderNetwork();
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
    if (await loadNetwork({ speedtest: true })) toast('Speed test complete', 'success');
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

async function rebootRouter() {
  const ok = await confirmAction({
    title: 'Reboot router?',
    message: 'The internet and every connection drop for about 5 minutes while the router restarts.',
    okLabel: 'Reboot',
    danger: true,
  });
  if (!ok) return;
  toast('Rebooting the router…');
  try {
    await jsonApi('/api/network/router/reboot', { method: 'POST' });
    toast('Reboot command accepted — the router will be down ~5 min', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Reboot failed: ' + (exc.message || exc), 'error');
    }
  }
}

// ----------------------------------------------------------------- wiring
export function wireNetworkControls() {
  initShowOfflinePref();
  initShowHiddenDevicesPref();
  initShowHiddenWifiPref();
  initDeviceSortPref();
  wireConfirmDialog();
  wireNetDeviceDetail();
  wireNetWifiDetail();
  if (els.netSpeedBtn) els.netSpeedBtn.addEventListener('click', runSpeedTest);
  if (els.netApReboot) els.netApReboot.addEventListener('click', rebootAccessPoint);
  if (els.netRouterReboot) els.netRouterReboot.addEventListener('click', rebootRouter);
  if (els.netOfflineToggle) els.netOfflineToggle.addEventListener('click', toggleShowOffline);
  if (els.netHiddenToggle) els.netHiddenToggle.addEventListener('click', toggleShowHiddenDevices);
  if (els.netWifiHiddenToggle) {
    els.netWifiHiddenToggle.addEventListener('click', toggleShowHiddenWifi);
  }
  if (els.netSortAlpha) els.netSortAlpha.addEventListener('click', function () { setDeviceSort('az'); });
  if (els.netSortSignal) els.netSortSignal.addEventListener('click', function () { setDeviceSort('signal'); });
  wireDhcpPlan();
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadNetwork);

// The AP SOAP read is expensive, so only poll while the Network tab is open.
export function onNetworkTab(tab) {
  if (tab === 'network') {
    loadNetwork();      // immediate refresh on entry (also the first load)
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}

export function restyleNetworkCharts() {
  restyleWifiChannelChart(state.wifiChart24);
  restyleWifiChannelChart(state.wifiChart5);
}
