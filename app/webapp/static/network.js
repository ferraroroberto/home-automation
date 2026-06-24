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

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
  NETWORK_SHOW_OFFLINE_KEY,
  NETWORK_DEVICE_SORT_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import {
  createWifiChannelChart,
  setWifiChannelData,
  restyleWifiChannelChart,
} from './charts.js';

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
// Coarse device category (from the backend heuristic) → Lucide sprite glyph.
// 'unknown' falls back to a neutral device glyph so every row is iconed alike.
const CATEGORY_ICONS = {
  phone: 'smartphone',
  computer: 'laptop',
  tv: 'tv',
  iot: 'cpu',
  nas: 'hard-drive',
  printer: 'printer',
  router: 'router',
  unknown: 'monitor-smartphone',
};
// Human band/connection label for the detail modal.
const CONN_LABELS = { '5GHz': '5 GHz', '2.4GHz': '2.4 GHz', wired: 'Wired' };
const WIFI_BAND_LABELS = { '2.4GHz': '2.4 GHz', '5GHz': '5 GHz', '6GHz': '6 GHz' };

function categoryIcon(category) {
  return CATEGORY_ICONS[category] || CATEGORY_ICONS.unknown;
}

let networkTimer = null;
let speedtestRunning = false;
let networkLoading = false;
// Last successful speed-test result, kept across polls (a normal poll returns
// null Mbps, which would otherwise wipe the displayed figure each cycle).
let lastSpeed = null;

// --------------------------------------------------------------- formatting
function fmtMs(v) { return v == null ? '—' : Math.round(Number(v)) + ' ms'; }
function fmtPct(v) { return v == null ? '—' : Math.round(Number(v)) + '%'; }
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
// "last seen Xh ago" from an epoch-seconds timestamp (Phase 4). Coarse on
// purpose — the registry updates only while the tab is open, so minute-level
// precision would be misleading.
function fmtAgo(epochSeconds) {
  if (epochSeconds == null) return 'unknown';
  const secs = Math.max(0, Math.floor(Date.now() / 1000) - Number(epochSeconds));
  if (secs < 90) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hours = Math.round(mins / 60);
  if (hours < 48) return hours + 'h ago';
  return Math.round(hours / 24) + 'd ago';
}
// Absolute-ish date for the detail modal's first-seen line.
function fmtDate(epochSeconds) {
  if (epochSeconds == null) return '—';
  try {
    return new Date(Number(epochSeconds) * 1000).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  } catch (_e) {
    return '—';
  }
}

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

function bandLabel(band) {
  return WIFI_BAND_LABELS[band] || band || '—';
}

function wifiSignalClass(signal) {
  if (signal == null) return '';
  if (signal < 60) return ' is-weak';
  if (signal >= 80) return ' is-strong';
  return '';
}

function wifiSummary(wifi) {
  if (!wifi || !wifi.available) return 'Unavailable';
  const parts = [];
  if (wifi.current_ssid) parts.push(wifi.current_ssid);
  if (wifi.current_band) parts.push(bandLabel(wifi.current_band));
  if (wifi.current_channel != null) parts.push('ch ' + wifi.current_channel);
  return parts.join(' · ') || 'Scan available';
}

function ensureWifiCharts() {
  if (!els.netWifiChart24 || !els.netWifiChart5) return;
  if (!state.wifiChart24) state.wifiChart24 = createWifiChannelChart(els.netWifiChart24, '2.4GHz');
  if (!state.wifiChart5) state.wifiChart5 = createWifiChannelChart(els.netWifiChart5, '5GHz');
}

function renderWifiRecommendations(items) {
  const list = items || [];
  els.netWifiRecommendations.innerHTML = '';
  if (!list.length) {
    els.netWifiRecommendations.hidden = true;
    return;
  }
  list.forEach(function (text) {
    const row = document.createElement('div');
    row.className = 'net-wifi-tip';
    row.textContent = text;
    els.netWifiRecommendations.appendChild(row);
  });
  els.netWifiRecommendations.hidden = false;
}

function wifiRow(b) {
  const row = document.createElement('div');
  row.className = 'net-wifi-row' + (b.connected ? ' is-current' : '') +
    wifiSignalClass(b.signal);

  const main = document.createElement('div');
  main.className = 'net-wifi-row-main';
  const name = document.createElement('span');
  name.className = 'net-wifi-row-name';
  name.textContent = b.ssid || '(hidden)';
  main.appendChild(name);
  if (b.connected) {
    const pill = document.createElement('span');
    pill.className = 'net-wifi-current';
    pill.textContent = 'current';
    main.appendChild(pill);
  }
  const meta = document.createElement('span');
  meta.className = 'net-wifi-row-meta';
  const metaBits = [bandLabel(b.band)];
  if (b.channel != null) metaBits.push('ch ' + b.channel);
  if (b.radio_type) metaBits.push(b.radio_type);
  if (b.authentication) metaBits.push(b.authentication);
  meta.textContent = metaBits.filter(Boolean).join(' · ');
  main.appendChild(meta);
  row.appendChild(main);

  const sig = document.createElement('span');
  sig.className = 'net-device-signal net-wifi-row-signal';
  if (b.signal != null) {
    const bar = document.createElement('span');
    bar.className = 'net-signal-bar';
    const fill = document.createElement('span');
    fill.className = 'net-signal-fill';
    fill.style.width = Math.max(0, Math.min(100, b.signal)) + '%';
    bar.appendChild(fill);
    sig.appendChild(bar);
    const pct = document.createElement('span');
    pct.className = 'net-signal-pct';
    pct.textContent = b.signal + '%';
    sig.appendChild(pct);
  } else {
    sig.textContent = '—';
  }
  row.appendChild(sig);

  const mac = document.createElement('span');
  mac.className = 'net-wifi-row-mac';
  mac.textContent = b.bssid || '—';
  row.appendChild(mac);
  return row;
}

function renderWifiList(bssids) {
  const list = (bssids || []).slice().sort(function (a, b) {
    const band = bandLabel(a.band).localeCompare(bandLabel(b.band));
    if (band !== 0) return band;
    return (b.signal || 0) - (a.signal || 0);
  });
  els.netWifiList.innerHTML = '';
  if (!list.length) {
    els.netWifiNote.hidden = false;
    els.netWifiNote.textContent = 'No visible Wi-Fi radios reported by this PC.';
    return;
  }
  els.netWifiNote.hidden = true;
  list.forEach(function (b) { els.netWifiList.appendChild(wifiRow(b)); });
}

function renderWifi(wifi) {
  const available = !!(wifi && wifi.available);
  const signal = wifi ? wifi.current_signal : null;
  els.netWifiStatus.textContent = signal != null ? signal + '%' : (available ? 'Scan' : 'Unavailable');
  els.netWifiStatus.className = 'net-wifi-status' + wifiSignalClass(signal);
  els.netWifiSummary.textContent = wifiSummary(wifi);

  if (!wifi) {
    els.netWifiMeta.textContent = '—';
    els.netWifiRecommendations.hidden = true;
    els.netWifiList.innerHTML = '';
    return;
  }

  const meta = [];
  if (wifi.interface_name) meta.push(wifi.interface_name);
  if (wifi.adapter_description && wifi.adapter_description !== wifi.interface_name) {
    meta.push(wifi.adapter_description);
  }
  if (wifi.current_radio_type) meta.push(wifi.current_radio_type);
  els.netWifiMeta.textContent = meta.length ? meta.join(' · ') : (wifi.error || 'Wi-Fi scan unavailable.');

  if (!available) {
    renderWifiRecommendations([]);
    els.netWifiList.innerHTML = '';
    els.netWifiNote.hidden = false;
    els.netWifiNote.textContent = wifi.error || 'Wi-Fi diagnostics are unavailable on this PC.';
    if (state.wifiChart24) setWifiChannelData(state.wifiChart24, []);
    if (state.wifiChart5) setWifiChannelData(state.wifiChart5, []);
    return;
  }

  ensureWifiCharts();
  const bssids = wifi.bssids || [];
  if (state.wifiChart24) {
    setWifiChannelData(state.wifiChart24, bssids.filter(function (b) { return b.band === '2.4GHz'; }));
  }
  if (state.wifiChart5) {
    setWifiChannelData(state.wifiChart5, bssids.filter(function (b) { return b.band === '5GHz'; }));
  }
  renderWifiRecommendations(wifi.recommendations || []);
  renderWifiList(bssids);
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
    ['5 GHz', counts['5GHz']],
    ['2.4 GHz', counts['2.4GHz']],
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

// Identity precedence: custom label → OUI vendor → reported hostname → MAC
// (issue #129 Phase 2). Most clients report an 'n/a' hostname, so the vendor
// and the rename are what make the list legible.
function deviceLabel(d) {
  return d.display_name || d.vendor || d.name || d.mac || '(unknown)';
}

function byNameThenSignal(a, b) {
  const label = deviceLabel(a).localeCompare(deviceLabel(b), undefined, { sensitivity: 'base' });
  if (label !== 0) return label;
  return bySignalThenName(a, b);
}

// Weakest signal first within a group; nulls (e.g. wired) sort last, then by name.
function bySignalThenName(a, b) {
  const sa = a.signal == null ? 1000 : a.signal;
  const sb = b.signal == null ? 1000 : b.signal;
  if (sa !== sb) return sa - sb;
  return deviceLabel(a).localeCompare(deviceLabel(b), undefined, { sensitivity: 'base' });
}

function sortDevices(list) {
  return list.slice().sort(state.networkDeviceSort === 'signal' ? bySignalThenName : byNameThenSignal);
}

function renderSortControls() {
  const isSignal = state.networkDeviceSort === 'signal';
  if (els.netSortAlpha) {
    els.netSortAlpha.classList.toggle('active', !isSignal);
    els.netSortAlpha.setAttribute('aria-pressed', isSignal ? 'false' : 'true');
  }
  if (els.netSortSignal) {
    els.netSortSignal.classList.toggle('active', isSignal);
    els.netSortSignal.setAttribute('aria-pressed', isSignal ? 'true' : 'false');
  }
}

function buildDeviceRow(d) {
  const offline = d.online === false;
  const row = document.createElement('div');
  row.className = 'net-device';
  const weak = !offline && d.is_wireless && d.signal != null && d.signal < WEAK_SIGNAL_PCT;
  if (weak) row.classList.add('is-weak');
  if (offline) row.classList.add('is-offline');

  const label = deviceLabel(d);
  // The name is a button that opens the detail/rename modal — mirrors the
  // detector/plug/presence rows. A leading category glyph gives identity at a
  // glance; the text ellipsises so long labels don't push the signal off-row.
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'net-device-name';
  name.title = 'Device details · rename';
  let inner = '<svg class="icon net-device-icon" aria-hidden="true"><use href="#i-' +
    categoryIcon(d.category) + '"></use></svg>';
  // A star marks a "mark important" device; appears on both online + offline rows.
  if (d.important) {
    inner += '<svg class="icon net-device-star" aria-hidden="true"><use href="#i-star"></use></svg>';
  }
  name.innerHTML = inner;
  const text = document.createElement('span');
  text.className = 'net-device-name-text';
  text.textContent = label;
  name.appendChild(text);
  // A small "new" pill for a device first seen in the last 24 h (Phase 4).
  if (d.is_new) {
    const pill = document.createElement('span');
    pill.className = 'net-device-new';
    pill.textContent = 'new';
    name.appendChild(pill);
  }
  name.addEventListener('click', function () { openNetDeviceDetail(d.mac); });
  row.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'net-device-meta';
  // IP, SSID for wireless clients, plus the vendor when it isn't already the
  // shown label (avoids "Apple · Apple").
  const metaBits = [d.ip || '—'];
  if (d.is_wireless && d.ssid) metaBits.push('Wi-Fi ' + d.ssid);
  if (d.vendor && label !== d.vendor) metaBits.push(d.vendor);
  meta.textContent = metaBits.join(' · ');
  row.appendChild(meta);

  const signal = document.createElement('span');
  signal.className = 'net-device-signal';
  if (offline) {
    // No live signal for an absent device — show how long ago it was last seen.
    signal.classList.add('net-device-lastseen');
    signal.textContent = fmtAgo(d.last_seen);
  } else if (d.signal != null) {
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

// Most-recently-seen first — the sort for the trailing "Offline" group.
function byLastSeenDesc(a, b) {
  return (b.last_seen || 0) - (a.last_seen || 0);
}

// The "Show offline" toggle is shown only when there are known-but-absent
// devices; it carries the count and mirrors the security/plugs toggle styling.
function renderOfflineToggle(offlineCount) {
  const btn = els.netOfflineToggle;
  if (!btn) return;
  btn.hidden = offlineCount === 0;
  btn.textContent = state.networkShowOffline ? 'Hide offline' : 'Show offline';
  btn.classList.toggle('active', state.networkShowOffline);
}

function renderDevices(devices) {
  const list = devices || [];
  els.netDevices.innerHTML = '';
  renderSortControls();
  const online = list.filter(function (d) { return d.online !== false; });
  const offline = list.filter(function (d) { return d.online === false; });
  renderOfflineToggle(offline.length);

  const showingOffline = state.networkShowOffline && offline.length > 0;
  if (!online.length && !showingOffline) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = state.network ? 'No attached devices reported.' : '—';
    return;
  }
  els.netDevicesNote.hidden = true;

  const seen = new Set();
  GROUPS.forEach(function (group) {
    const members = online.filter(function (d) { return d.conn_type === group.key; });
    members.forEach(function (d) { seen.add(d); });
    if (!members.length) return;
    appendGroup(group.label, sortDevices(members));
  });
  // Anything online with an unknown/missing conn_type lands in a trailing "Other".
  const other = online.filter(function (d) { return !seen.has(d); });
  if (other.length) appendGroup('Other', sortDevices(other));

  // Offline (known-but-absent) devices, newest-last-seen first, only when toggled.
  if (showingOffline) appendGroup('Offline', offline.slice().sort(byLastSeenDesc));
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
  renderWifi(net ? net.wifi : null);
  renderStats(net ? net.devices : []);
  renderDevices(net ? net.devices : []);
}

// ------------------------------------------------- device detail + rename
function deviceByMac(mac) {
  const list = (state.network && state.network.devices) || [];
  return list.find(function (d) { return d.mac === mac; }) || null;
}

function connText(d) {
  const base = CONN_LABELS[d.conn_type] || d.conn_type || '—';
  return d.link_rate ? base + ' · ' + d.link_rate + ' Mbps' : base;
}

function signalText(d) {
  if (d.signal != null) return d.signal + '%';
  return d.conn_type === 'wired' ? 'Wired' : '—';
}

// Render the Important switch from a device dict (Phase 4). Hidden for
// randomised MACs, which aren't tracked, so the flag would be meaningless.
function renderImportantToggle(d) {
  const btn = els.netDeviceImportant;
  if (!btn) return;
  if (els.netDeviceImportantRow) els.netDeviceImportantRow.hidden = !!d.randomized;
  const on = !!d.important;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = '<span class="knob"></span><span class="toggle-label">' +
    (on ? 'ON' : 'OFF') + '</span>';
}

function openNetDeviceDetail(mac) {
  const d = deviceByMac(mac);
  if (!d) return;
  state.selectedNetDeviceMac = mac;
  els.netDeviceDetailName.textContent = deviceLabel(d);
  // Status: online, or offline with how long since it was last on the network.
  els.netDeviceStatus.textContent = d.online === false
    ? 'Offline · last seen ' + fmtAgo(d.last_seen)
    : 'Online';
  els.netDeviceStatus.classList.toggle('is-offline', d.online === false);
  els.netDeviceVendor.textContent = d.vendor || '—';
  els.netDeviceIp.textContent = d.ip || '—';
  els.netDeviceConn.textContent = connText(d);
  els.netDeviceSignal.textContent = signalText(d);
  els.netDeviceSsid.textContent = d.ssid || '—';
  // First-seen + times-seen history (Phase 4); hidden for untracked randomised MACs.
  if (els.netDeviceSeenRow) {
    const tracked = !d.randomized && d.first_seen != null;
    els.netDeviceSeenRow.hidden = !tracked;
    if (tracked) {
      const times = d.times_seen != null ? d.times_seen + '×' : '';
      els.netDeviceSeen.textContent = 'since ' + fmtDate(d.first_seen) +
        (times ? ' · ' + times : '');
    }
  }
  els.netDeviceDisplayName.value = d.display_name || '';
  els.netDeviceDisplayName.placeholder = d.vendor || d.name || 'Custom label…';
  renderImportantToggle(d);
  // The MAC is the stable key the label maps back to; flag randomised ones so a
  // missing vendor / churning row is explained rather than mysterious.
  els.netDeviceMac.textContent = 'MAC: ' + (d.mac || '—') +
    (d.randomized ? ' · randomised address' : '');
  if (typeof els.netDeviceDialog.showModal === 'function') els.netDeviceDialog.showModal();
  else els.netDeviceDialog.setAttribute('open', '');
  els.netDeviceDisplayName.focus();
}

function closeNetDeviceDetail() {
  state.selectedNetDeviceMac = null;
  if (typeof els.netDeviceDialog.close === 'function') els.netDeviceDialog.close();
  else els.netDeviceDialog.removeAttribute('open');
}

async function saveNetDeviceName() {
  const mac = state.selectedNetDeviceMac;
  if (!mac) return;
  const newName = els.netDeviceDisplayName.value.trim();
  try {
    await jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    // Optimistic local update so the list + modal title reflect it without a
    // refetch (the next poll re-merges the same override server-side anyway).
    if (state.network && Array.isArray(state.network.devices)) {
      state.network.devices = state.network.devices.map(function (d) {
        return d.mac === mac ? Object.assign({}, d, { display_name: newName || null }) : d;
      });
    }
    const d = deviceByMac(mac);
    if (d) els.netDeviceDetailName.textContent = deviceLabel(d);
    renderNetwork();
    toast('Name saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

// Flip the Important flag for the open device (Phase 4). Optimistic: update the
// switch + local state immediately, then POST; the next poll re-merges the same
// flag server-side, and an offline important device starts alerting.
async function toggleImportant() {
  const mac = state.selectedNetDeviceMac;
  if (!mac) return;
  const d = deviceByMac(mac);
  if (!d || d.randomized) return;
  const next = !d.important;
  try {
    await jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/important', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ important: next }),
    });
    if (state.network && Array.isArray(state.network.devices)) {
      state.network.devices = state.network.devices.map(function (x) {
        return x.mac === mac ? Object.assign({}, x, { important: next }) : x;
      });
    }
    renderImportantToggle(deviceByMac(mac) || d);
    renderNetwork();
    toast(next ? 'Marked important' : 'Unmarked', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to update: ' + (exc.message || exc), 'error');
    }
  }
}

function wireNetDeviceDetail() {
  if (!els.netDeviceDialog) return;
  els.netDeviceDetailClose.addEventListener('click', closeNetDeviceDetail);
  els.netDeviceDialog.addEventListener('click', function (ev) {
    if (ev.target === els.netDeviceDialog) closeNetDeviceDetail();  // backdrop
  });
  els.netDeviceDisplayName.addEventListener('blur', saveNetDeviceName);
  els.netDeviceDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.netDeviceDisplayName.blur(); }
  });
  if (els.netDeviceImportant) els.netDeviceImportant.addEventListener('click', toggleImportant);
}

// Persisted "show offline" preference (localStorage), like plugs/security toggles.
function toggleShowOffline() {
  state.networkShowOffline = !state.networkShowOffline;
  try { localStorage.setItem(NETWORK_SHOW_OFFLINE_KEY, state.networkShowOffline ? '1' : '0'); }
  catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

function initShowOfflinePref() {
  try { state.networkShowOffline = localStorage.getItem(NETWORK_SHOW_OFFLINE_KEY) === '1'; }
  catch (_e) { state.networkShowOffline = false; }
}

function setDeviceSort(sort) {
  state.networkDeviceSort = sort === 'signal' ? 'signal' : 'az';
  try { localStorage.setItem(NETWORK_DEVICE_SORT_KEY, state.networkDeviceSort); }
  catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

function initDeviceSortPref() {
  try {
    state.networkDeviceSort =
      localStorage.getItem(NETWORK_DEVICE_SORT_KEY) === 'signal' ? 'signal' : 'az';
  } catch (_e) {
    state.networkDeviceSort = 'az';
  }
}

// ----------------------------------------------------------------- load
async function loadNetwork(opts) {
  const speedtest = !!(opts && opts.speedtest);
  if (networkLoading) return false;
  networkLoading = true;
  try {
    const url = speedtest ? '/api/network?speedtest=1' : '/api/network';
    state.network = await jsonApi(url);
    reportFetchOk('network');
    renderNetwork();
    return true;
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('network', exc, 'network');
    // Keep any last-good render in place; surface the reason in the device note.
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = exc.message || 'Failed to load network.';
    return false;
  } finally {
    networkLoading = false;
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

export function wireNetworkControls() {
  initShowOfflinePref();
  initDeviceSortPref();
  wireConfirmDialog();
  wireNetDeviceDetail();
  if (els.netSpeedBtn) els.netSpeedBtn.addEventListener('click', runSpeedTest);
  if (els.netApReboot) els.netApReboot.addEventListener('click', rebootAccessPoint);
  if (els.netRouterReboot) els.netRouterReboot.addEventListener('click', rebootRouter);
  if (els.netOfflineToggle) els.netOfflineToggle.addEventListener('click', toggleShowOffline);
  if (els.netSortAlpha) els.netSortAlpha.addEventListener('click', function () { setDeviceSort('az'); });
  if (els.netSortSignal) els.netSortSignal.addEventListener('click', function () { setDeviceSort('signal'); });
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

export function restyleNetworkCharts() {
  restyleWifiChannelChart(state.wifiChart24);
  restyleWifiChannelChart(state.wifiChart5);
}
