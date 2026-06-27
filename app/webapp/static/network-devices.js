/* Network tab — attached-device inventory + the per-device detail/rename modal.
 *
 * Split out of network.js (issue #197): the device list grouped by band (weakest
 * signal first), the sort and show-offline/show-hidden toggles with their
 * localStorage-backed prefs, and the rename / mark-important / hide detail modal.
 * The boot module (network.js) owns renderNetwork and calls into renderStats /
 * renderDevices here; this module calls back into renderNetwork after a mutation.
 */

'use strict';

import {
  state,
  els,
  toast,
  NETWORK_SHOW_OFFLINE_KEY,
  NETWORK_DEVICE_SORT_KEY,
  NETWORK_SHOW_HIDDEN_DEVICES_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, snapshotLabel } from './snapshots.js';
import { renderNetwork } from './network.js';

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
// Which source reported a device (issue #169) — shown only in the detail modal.
const SOURCE_LABELS = {
  ap: 'Access point',
  router: 'Router (DHCP)',
  both: 'Access point + Router',
  history: 'Last seen (offline)',
};

function categoryIcon(category) {
  return CATEGORY_ICONS[category] || CATEGORY_ICONS.unknown;
}

// --------------------------------------------------------------- formatting
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

// ----------------------------------------------------------------- render
export function renderStats(devices) {
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
  if (d.hidden) row.classList.add('is-hidden');

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

function renderDeviceHiddenToggle(hiddenCount) {
  if (els.netHiddenCount) {
    els.netHiddenCount.hidden = hiddenCount === 0;
    els.netHiddenCount.textContent = hiddenCount + ' hidden';
  }
  const btn = els.netHiddenToggle;
  if (!btn) return;
  btn.hidden = hiddenCount === 0;
  btn.textContent = state.networkShowHiddenDevices ? 'Hide' : 'Show hidden';
  btn.classList.toggle('active', state.networkShowHiddenDevices);
  btn.setAttribute('aria-pressed', state.networkShowHiddenDevices ? 'true' : 'false');
}

export function renderDevices(devices) {
  const all = devices || [];
  const hiddenCount = all.filter(function (d) { return !!d.hidden; }).length;
  const list = all.filter(function (d) {
    return state.networkShowHiddenDevices || !d.hidden;
  });
  els.netDevices.innerHTML = '';
  renderSortControls();
  const online = list.filter(function (d) { return d.online !== false; });
  const offline = list.filter(function (d) { return d.online === false; });
  renderOfflineToggle(offline.length);
  renderDeviceHiddenToggle(hiddenCount);

  const showingOffline = state.networkShowOffline && offline.length > 0;
  if (!online.length && !showingOffline) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = isSnapshotRestored('network')
      ? snapshotLabel('network')
      : (hiddenCount ? 'All attached devices are hidden.' : (state.network ? 'No attached devices reported.' : '—'));
    return;
  }
  if (isSnapshotRestored('network')) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = snapshotLabel('network');
  } else {
    els.netDevicesNote.hidden = true;
  }

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

function renderNetDeviceHiddenToggle(d) {
  const btn = els.netDeviceHiddenToggle;
  if (!btn) return;
  const on = !!d.hidden;
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
  els.netDeviceSource.textContent = SOURCE_LABELS[d.source] || d.source || '—';
  // Reported hostname stays visible even when a custom display name is set.
  els.netDeviceHostname.textContent = d.name || '—';
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
  renderNetDeviceHiddenToggle(d);
  netStaged = { important: !!d.important, hidden: !!d.hidden };
  clearNetDirty();
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
  netStaged = null;
  clearNetDirty();
  if (typeof els.netDeviceDialog.close === 'function') els.netDeviceDialog.close();
  else els.netDeviceDialog.removeAttribute('open');
}

// Detail-modal staging (#203): the display name, Important and Hidden edits are
// held locally and written only on Save. netStaged holds the working toggle
// state captured when the modal opens; closing discards it.
let netStaged = null;
let netDirty = false;

function markNetDirty() {
  netDirty = true;
  if (els.netDeviceSave) els.netDeviceSave.disabled = false;
}

function clearNetDirty() {
  netDirty = false;
  if (els.netDeviceSave) els.netDeviceSave.disabled = true;
}

function patchNetDevice(mac, patch) {
  if (state.network && Array.isArray(state.network.devices)) {
    state.network.devices = state.network.devices.map(function (d) {
      return d.mac === mac ? Object.assign({}, d, patch) : d;
    });
  }
}

// Toggles now only stage visually — the POST happens on Save.
function toggleImportant() {
  const d = deviceByMac(state.selectedNetDeviceMac);
  if (!d || d.randomized || !netStaged) return;
  netStaged.important = !netStaged.important;
  renderImportantToggle(Object.assign({}, d, { important: netStaged.important }));
  markNetDirty();
}

function toggleDeviceHidden() {
  const d = deviceByMac(state.selectedNetDeviceMac);
  if (!d || !netStaged) return;
  netStaged.hidden = !netStaged.hidden;
  renderNetDeviceHiddenToggle(Object.assign({}, d, { hidden: netStaged.hidden }));
  markNetDirty();
}

// Commit the staged edits; only fields that actually changed are sent. Optimistic
// local update mirrors what the next poll would re-merge server-side anyway.
async function saveNetDevice() {
  const mac = state.selectedNetDeviceMac;
  if (!mac || !netStaged) return;
  const d = deviceByMac(mac);
  if (!d) return;
  if (els.netDeviceSave) els.netDeviceSave.disabled = true;
  const newName = els.netDeviceDisplayName.value.trim();
  const ops = [];
  if ((d.display_name || '') !== newName) {
    ops.push(jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/display_name', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    }).then(function () { patchNetDevice(mac, { display_name: newName || null }); }));
  }
  if (!d.randomized && !!d.important !== netStaged.important) {
    ops.push(jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/important', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ important: netStaged.important }),
    }).then(function () { patchNetDevice(mac, { important: netStaged.important }); }));
  }
  if (!!d.hidden !== netStaged.hidden) {
    ops.push(jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/hidden', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden: netStaged.hidden }),
    }).then(function () { patchNetDevice(mac, { hidden: netStaged.hidden }); }));
  }
  try {
    await Promise.all(ops);
    const upd = deviceByMac(mac);
    if (upd) els.netDeviceDetailName.textContent = deviceLabel(upd);
    renderNetwork();
    clearNetDirty();
    toast('Saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save: ' + (exc.message || exc), 'error');
    }
    if (els.netDeviceSave) els.netDeviceSave.disabled = false;
  }
}

export function wireNetDeviceDetail() {
  if (!els.netDeviceDialog) return;
  els.netDeviceDetailClose.addEventListener('click', closeNetDeviceDetail);
  els.netDeviceDialog.addEventListener('click', function (ev) {
    if (ev.target === els.netDeviceDialog) closeNetDeviceDetail();  // backdrop
  });
  els.netDeviceDisplayName.addEventListener('input', markNetDirty);
  els.netDeviceDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveNetDevice(); }
  });
  if (els.netDeviceImportant) els.netDeviceImportant.addEventListener('click', toggleImportant);
  if (els.netDeviceHiddenToggle) els.netDeviceHiddenToggle.addEventListener('click', toggleDeviceHidden);
  if (els.netDeviceSave) els.netDeviceSave.addEventListener('click', saveNetDevice);
}

// ------------------------------------------------- prefs + toggles
// Persisted "show offline" preference (localStorage), like plugs/security toggles.
export function toggleShowOffline() {
  state.networkShowOffline = !state.networkShowOffline;
  try { localStorage.setItem(NETWORK_SHOW_OFFLINE_KEY, state.networkShowOffline ? '1' : '0'); }
  catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

export function toggleShowHiddenDevices(ev) {
  if (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  state.networkShowHiddenDevices = !state.networkShowHiddenDevices;
  try {
    localStorage.setItem(
      NETWORK_SHOW_HIDDEN_DEVICES_KEY,
      state.networkShowHiddenDevices ? '1' : '0'
    );
  } catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

export function initShowOfflinePref() {
  try { state.networkShowOffline = localStorage.getItem(NETWORK_SHOW_OFFLINE_KEY) === '1'; }
  catch (_e) { state.networkShowOffline = false; }
}

export function initShowHiddenDevicesPref() {
  try {
    state.networkShowHiddenDevices =
      localStorage.getItem(NETWORK_SHOW_HIDDEN_DEVICES_KEY) === '1';
  } catch (_e) {
    state.networkShowHiddenDevices = false;
  }
}

export function setDeviceSort(sort) {
  state.networkDeviceSort = sort === 'signal' ? 'signal' : 'az';
  try { localStorage.setItem(NETWORK_DEVICE_SORT_KEY, state.networkDeviceSort); }
  catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

export function initDeviceSortPref() {
  try {
    state.networkDeviceSort =
      localStorage.getItem(NETWORK_DEVICE_SORT_KEY) === 'signal' ? 'signal' : 'az';
  } catch (_e) {
    state.networkDeviceSort = 'az';
  }
}
