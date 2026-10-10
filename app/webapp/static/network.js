/* Network (LAN) controller — boot/core.
 *
 * Owns the home-network view's top-level shell: the Devices tab's Network
 * group rows (#884, decision 7 of #872; Settings from #779), the Network sheet
 * they open, internet health (+ opt-in speed test), AP/router health with the
 * confirm-gated reboots, the poll lifecycle, and the renderNetwork
 * orchestrator. The confirm dialog itself is
 * the neutral `./confirm.js` primitive (issue #574) — it used to live here,
 * which made unrelated modules import this feature tab to get it. The
 * feature panels live in sibling modules and are wired in here (issue #197):
 *   ./network-devices.js — attached-device inventory + detail/rename modal
 *   ./network-groups.js  — device-group rename/delete dialog (#702)
 *   ./network-wifi.js    — Wi-Fi diagnostics + channel charts + Wi-Fi modal
 *   ./network-survey.js  — Wi-Fi walk test: per-room coverage samples (#547)
 *   ./network-dhcp.js    — DHCP reservation planner + apply flow
 * Read-mostly — it reads GET /api/network and writes only the AP/router reboots.
 *
 * Cadence: the AP SOAP read is comparatively expensive, so entering the
 * Devices tab reads once (the group rows' facts) and the read repeats only
 * while the Network sheet is open.
 * The speed test never auto-runs — it is an explicit button that adds ~13 s.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchOk,
} from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { esc, fmtPct, friendlyError } from './format.js';
import { icon } from './_vendored/icons/icons.js';
import {
  restoreSnapshot,
  saveSnapshot,
} from './snapshots.js';
import { restyleWifiChannelChart } from './charts.js';
import { createPoller } from './poll.js';
import { createViewState, markTabFailure, renderFeedback } from './view-state.js';
import {
  renderStats,
  renderDevices,
  wireNetDeviceDetail,
  toggleShowOffline,
  toggleShowHiddenDevices,
  setDeviceSort,
  setDeviceGrouping,
  hasLiveLink,
  WEAK_SIGNAL_PCT,
  initShowOfflinePref,
  initShowHiddenDevicesPref,
  initDeviceSortPref,
  initDeviceGroupingPref,
} from './network-devices.js';
import { wireNetGroupDialog } from './network-groups.js';
import { loadInternetTrends } from './network-trends.js';
import { loadNightlyPref, wireNightlyToggle } from './network-speedtest.js';
import {
  renderWifi,
  wireNetWifiDetail,
  toggleShowHiddenWifi,
  initShowHiddenWifiPref,
} from './network-wifi.js';
import {
  renderSurvey,
  wireSurvey,
  initSurveyMacPref,
} from './network-survey.js';
import { wireDhcpPlan } from './network-dhcp.js';
import { confirmAction } from './confirm.js';
import { sheet } from './sheet.js';

const POLL_MS = 15_000;

let speedtestRunning = false;
let networkLoading = false;
const networkView = createViewState('network');
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

function renderNetworkFeedback() {
  if (!els.paneNetwork) return;
  renderFeedback(networkView, els.netFeedback, {
    paneEl: els.paneNetwork,
    icon: 'wifi',
    loadingLabel: 'Reading network status…',
    errorLabel: 'Network unavailable',
    snapshotKey: 'network',
    onRetry: function () { loadNetwork(); },
  });
}

function disableStaleNetworkActions() {
  [els.netApReboot, els.netRouterReboot].forEach(function (button) {
    if (button) button.disabled = true;
  });
}

function markNetworkFailure() {
  markTabFailure(networkView, {
    hasData: !!state.network,
    scope: 'network',
    label: 'network',
    render: function () {
      renderNetworkFeedback();
      if (state.network) disableStaleNetworkActions();
    },
  });
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
    // Lucide arrows for down/up (#779), each with a hidden word for readers.
    const seg = [
      icon('arrow-down') + '<span class="visually-hidden">Download </span>' + esc(fmtMbps(lastSpeed.down)),
      icon('arrow-up') + '<span class="visually-hidden">Upload </span>' + esc(fmtMbps(lastSpeed.up)),
    ];
    if (lastSpeed.server) seg.push('via ' + esc(lastSpeed.server));
    els.netSpeedResult.innerHTML = seg.join(' · ');
    els.netSpeedResult.hidden = false;
  } else {
    els.netSpeedResult.hidden = true;
  }
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
    els.netApMeta.textContent = 'Unreachable' + (ap && ap.error ? ': ' + friendlyError(ap.error, 'no response') : '');
    els.netApMeta.classList.add('is-error');
    els.netApReboot.hidden = true;  // can't reboot what we can't reach
  }

  // Router — reachability, login, and the Phase-3 WAN/internet status.
  els.netRouterName.textContent = (router && router.model) || 'Router';
  const routerReachable = !!(router && router.reachable);
  const routerAuthed = !!(router && router.authenticated);
  if (!routerReachable) {
    els.netRouterMeta.textContent = 'Unreachable' + (router && router.error ? ': ' + friendlyError(router.error, 'no response') : '');
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

// The Devices tab's Network group (#884): the internet in one line with the
// connected badge, and the attached devices as a count with the weak ones.
function renderGroupRows(net) {
  if (!els.networkInternetMeta) return;
  const internet = net ? net.internet : null;
  const avatar = els.networkInternetAvatar;
  if (!internet) {
    els.networkInternetMeta.textContent = networkView.state === 'error'
      ? 'Unavailable'
      : (net ? 'No data' : 'Reading…');
    if (avatar) delete avatar.dataset.badge;
  } else if (!internet.online) {
    els.networkInternetMeta.textContent = 'Offline';
    if (avatar) avatar.dataset.badge = 'down';
  } else {
    const parts = [];
    if (internet.external_ms != null) parts.push(fmtMs(internet.external_ms));
    if (lastSpeed) {
      parts.push(icon('arrow-down', 'row-meta-arrow') + '<span class="visually-hidden">Download </span>' +
        esc(String(Math.round(Number(lastSpeed.down)))) + ' ' +
        icon('arrow-up', 'row-meta-arrow') + '<span class="visually-hidden">Upload </span>' +
        esc(fmtMbps(lastSpeed.up)));
    }
    els.networkInternetMeta.innerHTML = parts.length ? parts.join(' · ') : 'Online';
    if (avatar) avatar.dataset.badge = 'up';
  }
  const devices = net ? (net.devices || []).filter(function (d) { return !d.hidden; }) : [];
  if (!els.networkDevicesMeta) return;
  if (!net) {
    els.networkDevicesMeta.textContent = '';
    return;
  }
  const live = devices.filter(hasLiveLink);
  const weak = live.filter(function (d) {
    return d.is_wireless && d.signal != null && d.signal < WEAK_SIGNAL_PCT;
  }).length;
  els.networkDevicesMeta.textContent = live.length + ' online' + (weak ? ' · ' + weak + ' weak' : '');
}

function renderNetwork() {
  const net = state.network;
  renderGroupRows(net);
  renderInternet(net ? net.internet : null);
  renderHealth(net ? net.access_point : null, net ? net.router : null);
  // The Wi-Fi charts load Chart.js on first use (#760): only once the sheet
  // that shows them is open, never for the group rows alone.
  if (networkSheet.isOpen()) renderWifi(net ? net.wifi : null);
  renderSurvey();
  renderStats(net
    ? (net.devices || []).filter(function (d) { return state.networkShowHiddenDevices || !d.hidden; })
    : []);
  renderDevices(net ? net.devices : []);
  renderNetworkFeedback();
  if (networkView.state === 'stale' && networkView.liveUnavailable) {
    disableStaleNetworkActions();
  }
}

// The Network sheet (sheet.js, instant: reboots, the speed test and the
// toggles act as they are pressed; renames open their own staged dialogs).
const networkSheet = sheet(els.networkSheet, {
  model: 'instant',
  closeButton: els.networkSheetClose,
  doneButton: els.networkSheetDone,
  onClose: function () { updatePolling(); },
});

// Open the sheet, optionally at one of its cards (a disclosure opens).
function openNetworkSheet(trigger, card) {
  networkSheet.open(trigger);
  renderNetwork();
  loadNightlyPref();
  updatePolling();
  if (card) {
    if (card.tagName === 'DETAILS') card.open = true;
    requestAnimationFrame(function () { card.scrollIntoView({ block: 'start' }); });
  } else if (els.networkSheet) {
    const body = els.networkSheet.querySelector('.detail-card');
    if (body) body.scrollTop = 0;
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
    networkView.set('loading', { liveUnavailable: false });
    renderNetworkFeedback();
  }
  try {
    const url = speedtest ? '/api/network?speedtest=1' : '/api/network';
    state.network = await jsonApi(url);
    reportFetchOk('network');
    if (!speedtest) saveSnapshot('network', state.network);
    networkView.set('ready', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderNetwork();
    // Trends are context under the live figures: fetched after the read that
    // just recorded a sample, never blocking or failing the tile itself.
    loadInternetTrends();
    return true;
  } catch (exc) {
    if (isAuthRequired(exc)) return;
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
  networkView.set('stale', {
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
    reportActionFailure(exc, 'Speed test failed');
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
    reportActionFailure(exc, 'Reboot failed');
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
    reportActionFailure(exc, 'Reboot failed');
  }
}

// ----------------------------------------------------------------- wiring
export function wireNetworkControls() {
  initShowOfflinePref();
  initShowHiddenDevicesPref();
  initShowHiddenWifiPref();
  initSurveyMacPref();
  initDeviceSortPref();
  initDeviceGroupingPref();
  wireNetDeviceDetail();
  wireNetGroupDialog();
  wireNetWifiDetail();
  wireSurvey();
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
  if (els.netGroupByBand) {
    els.netGroupByBand.addEventListener('click', function () { setDeviceGrouping('band'); });
  }
  if (els.netGroupByGroup) {
    els.netGroupByGroup.addEventListener('click', function () { setDeviceGrouping('group'); });
  }
  wireNightlyToggle();
  wireDhcpPlan();
  if (els.networkInternetOpen) {
    els.networkInternetOpen.addEventListener('click', function () {
      openNetworkSheet(els.networkInternetOpen, null);
    });
  }
  if (els.networkDevicesOpen) {
    els.networkDevicesOpen.addEventListener('click', function () {
      openNetworkSheet(els.networkDevicesOpen, document.querySelector('.net-devices-card'));
    });
  }
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadNetwork);

// The AP SOAP read is expensive: one read on entering the Devices tab feeds
// the group rows, and the poll runs only while the Network sheet is open.
let activeTab = null;

function updatePolling() {
  schedule(activeTab === 'iot' && networkSheet.isOpen() ? POLL_MS : 0);
}

export function onNetworkTab(tab) {
  activeTab = tab;
  if (tab === 'iot') loadNetwork();
  updatePolling();
}

export function restyleNetworkCharts() {
  restyleWifiChannelChart(state.wifiChart24);
  restyleWifiChannelChart(state.wifiChart5);
}
