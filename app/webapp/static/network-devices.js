/* Network tab — attached-device inventory + the per-device detail/rename modal.
 *
 * Split out of network.js (issue #197): the device list grouped by band (weakest
 * signal first), the sort and show-offline/show-hidden toggles with their
 * localStorage-backed prefs, and the rename / mark-important / hide detail modal.
 * The boot module (network.js) owns renderNetwork and calls into renderStats /
 * renderDevices here; this module calls back into renderNetwork after a mutation.
 *
 * Issue #513 adds a second way to group the same list: the user's own device
 * groups instead of the radio band, with a trailing synthetic "Unclassified"
 * bucket and a rename/delete dialog on each real group header. Issue #519
 * makes "My groups" the default view and extends the offline show/hide toggle
 * to it too — a group's online/total count still reflects every member, but
 * the offline rows themselves are only rendered (shaded, in place) when the
 * toggle is on, same as the band view. A group left with nothing visible after
 * that filter (all members offline, toggle off) is simply skipped rather than
 * rendering an empty header.
 *
 * Issue #550 splits "in this read" from "demonstrably on the network". The
 * router's DHCP lease table outlives the client that held the lease, so a row
 * can come back with online:true yet no band, no SSID and no signal — a phone
 * that left hours ago still holds its lease. Visibility and the group counts
 * therefore key off hasLiveLink() below, not the raw online flag, and those
 * no-link rows ride the same Offline toggle as the genuinely absent ones.
 */

'use strict';

import {
  state,
  els,
  toast,
  NETWORK_SHOW_OFFLINE_KEY,
  NETWORK_DEVICE_SORT_KEY,
  NETWORK_DEVICE_GROUPING_KEY,
  NETWORK_SHOW_HIDDEN_DEVICES_KEY,
  persistedFlag,
  persistedPref,
} from './state.js';
import { jsonApi } from './api.js';
import { renderSignalBar } from './format.js';
import { isSnapshotRestored, snapshotLabel } from './snapshots.js';
import { renderNetwork, confirmAction } from './network.js';
import { toggleMarkup } from './toggle.js';
import { closeDialog, openDialog } from './dialog.js';

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
// The synthetic catch-all in the "My groups" view. Never persisted, never
// renamable or deletable, and always rendered last — it is simply whatever has
// no explicit assignment.
const UNCLASSIFIED = 'Unclassified';
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
export function deviceLabel(d) {
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

// Band + SSID as reported now, falling back to the last-known values an offline
// row carries (the live fields are null once a device stops being observed).
function bandOf(d) { return d.conn_type || d.last_conn_type || null; }
function ssidOf(d) { return d.ssid || d.last_ssid || null; }
function bandLabel(d) {
  const band = bandOf(d);
  return band ? (CONN_LABELS[band] || band) : '—';
}

// Is this device demonstrably on the network right now (issue #550)?
//
// Being in the current read is not enough: a device known only from the
// router's DHCP lease table comes back with online:true but conn_type, ssid and
// signal all null, and that lease survives the client leaving (the caveat
// src.network_router.read_dhcp_leases documents for issue #507). Nor can the
// backend classify those rows away — the lease's PhyPortName reads LAN4 for
// every client behind the access point, wired or not, and some lease-only rows
// do answer a ping, so they are neither reliably wired nor reliably gone.
//
// So the list asks for evidence of the link instead: a signal reading, or a
// wired connection. Anything else is "no link" — not claimed offline, just not
// shown while the Offline toggle is hiding what can't be vouched for.
//
// A no-link device can still be positively confirmed host-side: the backend
// pings it (issue #552, bounded to this subset, cached briefly) and reports
// `ping_reachable`. A confirmed device promotes into the live view too, with
// its own marker (buildDeviceRow below) so it reads as probe-confirmed rather
// than AP/router-confirmed.
function hasLiveLink(d) {
  return d.online !== false && (d.signal != null || d.conn_type === 'wired' || d.ping_reachable === true);
}

function renderGroupingControls() {
  const byGroup = state.networkDeviceGrouping === 'group';
  if (els.netGroupByBand) {
    els.netGroupByBand.classList.toggle('active', !byGroup);
    els.netGroupByBand.setAttribute('aria-pressed', byGroup ? 'false' : 'true');
  }
  if (els.netGroupByGroup) {
    els.netGroupByGroup.classList.toggle('active', byGroup);
    els.netGroupByGroup.setAttribute('aria-pressed', byGroup ? 'true' : 'false');
  }
}

// True only when a row is live *because of* the ping probe — i.e. it has no
// AP/router evidence of its own (#552). Distinguishes the probe-confirmed
// badge from an ordinary live device that merely happens to carry the flag.
function pingConfirmed(d) {
  return d.online !== false && d.ping_reachable === true && d.signal == null && d.conn_type !== 'wired';
}

function buildDeviceRow(d, grouped) {
  const offline = d.online === false;
  // In the read, but with nothing to show for it (#550) — dimmed like an
  // offline row, since the toggle groups the two together. A ping-confirmed
  // device (#552) has live evidence of its own now, so it is never no-link.
  const noLink = !offline && !hasLiveLink(d);
  const row = document.createElement('div');
  row.className = 'net-device' + (grouped ? ' is-grouped' : '');
  const weak = !offline && d.is_wireless && d.signal != null && d.signal < WEAK_SIGNAL_PCT;
  if (weak) row.classList.add('is-weak');
  if (offline) row.classList.add('is-offline');
  if (noLink) row.classList.add('is-nolink');
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
  // Marks a row promoted purely by the ping probe (#552) — distinct from
  // AP/router evidence, so it doesn't read as an ordinary live client.
  if (pingConfirmed(d)) {
    const pill = document.createElement('span');
    pill.className = 'net-device-reachable';
    pill.textContent = 'reachable';
    name.appendChild(pill);
  }
  name.addEventListener('click', function () { openNetDeviceDetail(d.mac); });
  row.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'net-device-meta';
  if (grouped) {
    // The grouped view answers "which of these is up, and on what?" — so band
    // and SSID are explicit (the band header no longer says it). The MAC is
    // dropped (#519) for consistency with the band view; it's still reachable
    // from the detail modal.
    const bits = [d.ip || '—', bandLabel(d)];
    const ssid = ssidOf(d);
    if (ssid) bits.push('Wi-Fi ' + ssid);
    meta.textContent = bits.join(' · ');
  } else {
    // IP, SSID for wireless clients, plus the vendor when it isn't already the
    // shown label (avoids "Apple · Apple").
    const metaBits = [d.ip || '—'];
    if (d.is_wireless && d.ssid) metaBits.push('Wi-Fi ' + d.ssid);
    if (d.vendor && label !== d.vendor) metaBits.push(d.vendor);
    meta.textContent = metaBits.join(' · ');
  }
  row.appendChild(meta);

  const signal = document.createElement('span');
  signal.className = 'net-device-signal';
  if (offline) {
    // No live signal for an absent device — show how long ago it was last seen.
    signal.classList.add('net-device-lastseen');
    signal.textContent = fmtAgo(d.last_seen);
  } else if (d.signal != null) {
    signal.appendChild(renderSignalBar(d.signal));
  } else if (d.conn_type === 'wired') {
    signal.textContent = 'wired';
  } else if (d.ping_reachable === true) {
    // Promoted by the host-side probe (#552), not AP/router evidence — its own
    // distinct, non-dimmed treatment so it doesn't read as an ordinary signal.
    signal.classList.add('net-device-pingreachable');
    signal.textContent = 'reachable via ping';
  } else {
    // Says why the row is behind the Offline toggle, where a bare "—" read as
    // a missing measurement on an otherwise-live device (#550).
    signal.classList.add('net-device-nolink');
    signal.textContent = 'no link';
  }
  row.appendChild(signal);
  return row;
}

// Most-recently-seen first — the sort for the trailing "Offline" group.
function byLastSeenDesc(a, b) {
  return (b.last_seen || 0) - (a.last_seen || 0);
}

// The offline toggle is shown only when it has something to reveal — the
// known-but-absent devices plus the no-link ones (#550) — and mirrors the
// security/plugs toggle styling. The label always reflects current visibility
// (#519), never the pending action — "Offline shown" while those rows are
// visible, "Offline hidden" while they're filtered out.
function renderOfflineToggle(dormantCount) {
  const btn = els.netOfflineToggle;
  if (!btn) return;
  btn.hidden = dormantCount === 0;
  btn.textContent = state.networkShowOffline ? 'Offline shown' : 'Offline hidden';
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

// The note under the list: the snapshot label when the data is restored, or the
// reason the list is empty. Returns true when there is nothing left to render,
// so both grouping modes can bail on the same line.
function renderDevicesNote(hasRows, hiddenCount) {
  if (!hasRows) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = isSnapshotRestored('network')
      ? snapshotLabel('network')
      : (hiddenCount ? 'All attached devices are hidden.' : (state.network ? 'No attached devices reported.' : '—'));
    return true;
  }
  if (isSnapshotRestored('network')) {
    els.netDevicesNote.hidden = false;
    els.netDevicesNote.textContent = snapshotLabel('network');
  } else {
    els.netDevicesNote.hidden = true;
  }
  return false;
}

export function renderDevices(devices) {
  const all = devices || [];
  const hiddenCount = all.filter(function (d) { return !!d.hidden; }).length;
  const list = all.filter(function (d) {
    return state.networkShowHiddenDevices || !d.hidden;
  });
  els.netDevices.innerHTML = '';
  renderSortControls();
  renderGroupingControls();
  const byGroup = state.networkDeviceGrouping === 'group';
  // Live = evidence of a link right now; dormant = everything the Offline
  // toggle governs, i.e. the absent rows *and* the no-link ones (#550).
  const live = list.filter(hasLiveLink);
  const dormant = list.filter(function (d) { return !hasLiveLink(d); });
  // The offline toggle behaves identically in both grouping modes (#519).
  renderOfflineToggle(dormant.length);
  renderDeviceHiddenToggle(hiddenCount);

  if (byGroup) {
    renderCustomGroups(list, hiddenCount);
    return;
  }

  const showingDormant = state.networkShowOffline && dormant.length > 0;
  if (renderDevicesNote(live.length > 0 || showingDormant, hiddenCount)) return;

  const seen = new Set();
  GROUPS.forEach(function (group) {
    const members = live.filter(function (d) { return d.conn_type === group.key; });
    members.forEach(function (d) { seen.add(d); });
    if (!members.length) return;
    appendGroup(group.label, sortDevices(members));
  });
  // Anything live with an unknown/missing conn_type lands in a trailing "Other".
  const other = live.filter(function (d) { return !seen.has(d); });
  if (other.length) appendGroup('Other', sortDevices(other));

  if (showingDormant) {
    // Two distinct states, so two groups rather than one mixed bucket: "No
    // link" is still in the read but unvouched-for, "Offline" is genuinely
    // absent (newest-last-seen first).
    const noLink = dormant.filter(function (d) { return d.online !== false; });
    const offline = dormant.filter(function (d) { return d.online === false; });
    if (noLink.length) appendGroup('No link', sortDevices(noLink));
    if (offline.length) appendGroup('Offline', offline.slice().sort(byLastSeenDesc));
  }
}

function appendGroup(label, members) {
  const head = document.createElement('h4');
  head.className = 'net-group-head';
  head.textContent = label + ' · ' + members.length;
  els.netDevices.appendChild(head);
  members.forEach(function (d) { els.netDevices.appendChild(buildDeviceRow(d, false)); });
}

// ------------------------------------------------------------- group view
// The set of groups is derived from the device list alone: no registry, so an
// empty group cannot exist and the last device leaving one makes it disappear.
// Unclassified is appended last and only when it has members.
function groupsOf(list) {
  const names = [];
  const seen = new Set();
  list.forEach(function (d) {
    const name = (d.group || '').trim();
    if (!name || seen.has(name)) return;
    seen.add(name);
    names.push(name);
  });
  names.sort(function (a, b) {
    return a.localeCompare(b, undefined, { sensitivity: 'base' });
  });
  return names;
}

function membersOf(list, name) {
  return list.filter(function (d) { return (d.group || '').trim() === name; });
}

// Live first, then the chosen sort — an offline or no-link row stays in its
// group but sinks below the live ones rather than splitting the group in two.
function byOnlineThenSort(a, b) {
  const oa = hasLiveLink(a) ? 0 : 1;
  const ob = hasLiveLink(b) ? 0 : 1;
  if (oa !== ob) return oa - ob;
  return state.networkDeviceSort === 'signal'
    ? bySignalThenName(a, b)
    : byNameThenSignal(a, b);
}

function renderCustomGroups(list, hiddenCount) {
  // The offline toggle now governs visibility here too (#519): a group's
  // online/total count still counts every member, but an offline or no-link
  // row only renders (shaded, in place) when the toggle is on.
  const showOffline = state.networkShowOffline;
  const visibleTotal = list.filter(function (d) {
    return showOffline || hasLiveLink(d);
  }).length;
  if (renderDevicesNote(visibleTotal > 0, hiddenCount)) return;

  groupsOf(list).forEach(function (name) {
    appendCustomGroup(name, membersOf(list, name), true, showOffline);
  });
  const rest = list.filter(function (d) { return !(d.group || '').trim(); });
  if (rest.length) appendCustomGroup(UNCLASSIFIED, rest, false, showOffline);
}

function appendCustomGroup(name, members, editable, showOffline) {
  // The count reflects every member regardless of the offline toggle; only
  // the rendered rows are filtered by it. A group left with nothing visible
  // (no member has a live link, toggle off) is skipped rather than rendering
  // an empty header (#519). "Online" counts a live link, not merely presence
  // in the read — four phones holding stale leases read 0/4, not 4/4 (#550).
  const onlineCount = members.filter(hasLiveLink).length;
  const visible = showOffline ? members : members.filter(hasLiveLink);
  if (!visible.length) return;
  const head = document.createElement('h4');
  head.className = 'net-group-head net-group-head--custom';
  const title = document.createElement('span');
  title.textContent = name;
  head.appendChild(title);
  const count = document.createElement('span');
  count.className = 'net-group-head-count';
  // "3/4 online" answers the group-level question without expanding anything.
  count.textContent = '· ' + onlineCount + '/' + members.length + ' online';
  head.appendChild(count);
  if (editable) {
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'net-group-edit';
    edit.title = 'Rename or delete this group';
    edit.setAttribute('aria-label', 'Edit group ' + name);
    edit.dataset.group = name;
    edit.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-pencil"></use></svg>';
    edit.addEventListener('click', function () { openGroupDialog(name); });
    head.appendChild(edit);
  }
  els.netDevices.appendChild(head);
  visible.slice().sort(byOnlineThenSort).forEach(function (d) {
    els.netDevices.appendChild(buildDeviceRow(d, true));
  });
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
  if (d.conn_type === 'wired') return 'Wired';
  if (d.online !== false && d.ping_reachable === true) return 'Reachable via ping';
  return d.online === false ? '—' : 'No link';
}

// Render the Important switch from a device dict (Phase 4). Hidden for
// randomised MACs, which aren't tracked, so the flag would be meaningless.
// Sentinel option value: picking it reveals the free-text field, which is how a
// group is created — there is no "create empty group" step, since an empty group
// cannot exist.
const NEW_GROUP = '__new__';

function allGroupNames() {
  return groupsOf((state.network && state.network.devices) || []);
}

function renderGroupPicker(d) {
  const select = els.netDeviceGroup;
  if (!select) return;
  // Every existing group is offered; the device's own group is necessarily
  // among them, since the list is derived from the same device array.
  const current = (d.group || '').trim();
  const names = allGroupNames();
  select.innerHTML = '';
  const options = [['', UNCLASSIFIED]]
    .concat(names.map(function (n) { return [n, n]; }))
    .concat([[NEW_GROUP, 'New group…']]);
  options.forEach(function (pair) {
    const opt = document.createElement('option');
    opt.value = pair[0];
    opt.textContent = pair[1];
    select.appendChild(opt);
  });
  select.value = current;
  if (els.netDeviceGroupNew) els.netDeviceGroupNew.value = '';
  syncGroupNewRow();
}

function syncGroupNewRow() {
  const creating = els.netDeviceGroup && els.netDeviceGroup.value === NEW_GROUP;
  if (els.netDeviceGroupNewRow) els.netDeviceGroupNewRow.hidden = !creating;
  return creating;
}

// The group the modal would save right now — the picked one, or the typed name
// when "New group…" is selected.
function stagedGroup() {
  if (!els.netDeviceGroup) return '';
  if (els.netDeviceGroup.value !== NEW_GROUP) return els.netDeviceGroup.value;
  return els.netDeviceGroupNew ? els.netDeviceGroupNew.value.trim() : '';
}

function renderImportantToggle(d) {
  const btn = els.netDeviceImportant;
  if (!btn) return;
  if (els.netDeviceImportantRow) els.netDeviceImportantRow.hidden = !!d.randomized;
  const on = !!d.important;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
}

function renderNetDeviceHiddenToggle(d) {
  const btn = els.netDeviceHiddenToggle;
  if (!btn) return;
  const on = !!d.hidden;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
}

function openNetDeviceDetail(mac) {
  const d = deviceByMac(mac);
  if (!d) return;
  state.selectedNetDeviceMac = mac;
  els.netDeviceDetailName.textContent = deviceLabel(d);
  // Status: online, offline with how long since it was last on the network,
  // or "no live link" for a row the router still leases but nothing can
  // vouch for (#550) — deliberately not called Offline, which would claim a
  // fact the read never established.
  const live = hasLiveLink(d);
  if (d.online === false) {
    els.netDeviceStatus.textContent = 'Offline · last seen ' + fmtAgo(d.last_seen);
  } else if (!live) {
    els.netDeviceStatus.textContent = 'No live link · leased, but not seen on any radio';
  } else {
    els.netDeviceStatus.textContent = 'Online';
  }
  els.netDeviceStatus.classList.toggle('is-offline', !live);
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
  renderGroupPicker(d);
  renderImportantToggle(d);
  renderNetDeviceHiddenToggle(d);
  netStaged = { important: !!d.important, hidden: !!d.hidden };
  clearNetDirty();
  // The MAC is the stable key the label maps back to; flag randomised ones so a
  // missing vendor / churning row is explained rather than mysterious.
  els.netDeviceMac.textContent = 'MAC: ' + (d.mac || '—') +
    (d.randomized ? ' · randomised address' : '');
  openDialog(els.netDeviceDialog);
  els.netDeviceDisplayName.focus();
}

function closeNetDeviceDetail() {
  state.selectedNetDeviceMac = null;
  netStaged = null;
  clearNetDirty();
  closeDialog(els.netDeviceDialog);
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
  const newGroup = stagedGroup();
  if ((d.group || '') !== newGroup) {
    ops.push(jsonApi('/api/network/devices/' + encodeURIComponent(mac) + '/group', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group: newGroup }),
    }).then(function () { patchNetDevice(mac, { group: newGroup || null }); }));
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
  if (els.netDeviceGroup) {
    els.netDeviceGroup.addEventListener('change', function () {
      if (syncGroupNewRow() && els.netDeviceGroupNew) els.netDeviceGroupNew.focus();
      markNetDirty();
    });
  }
  if (els.netDeviceGroupNew) {
    els.netDeviceGroupNew.addEventListener('input', markNetDirty);
    els.netDeviceGroupNew.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); saveNetDevice(); }
    });
  }
}

// -------------------------------------------------- group rename / delete
// The dialog only ever edits a real (persisted) group; Unclassified has no
// pencil, so there is no path here for it.
let selectedGroup = null;

function openGroupDialog(name) {
  if (!els.netGroupDialog) return;
  selectedGroup = name;
  const members = membersOf((state.network && state.network.devices) || [], name);
  els.netGroupDialogTitle.textContent = name;
  els.netGroupMembers.textContent = members.length +
    (members.length === 1 ? ' device' : ' devices');
  els.netGroupName.value = name;
  if (els.netGroupSave) els.netGroupSave.disabled = true;
  openDialog(els.netGroupDialog);
  els.netGroupName.focus();
}

function closeGroupDialog() {
  selectedGroup = null;
  closeDialog(els.netGroupDialog);
}

// Rewrite the group on every local device row so the list re-renders without
// waiting for the next poll — the same optimistic pattern as the device modal.
function patchGroupLocally(from, to) {
  if (!(state.network && Array.isArray(state.network.devices))) return;
  state.network.devices = state.network.devices.map(function (d) {
    return (d.group || '') === from ? Object.assign({}, d, { group: to || null }) : d;
  });
}

async function saveGroupName() {
  const name = selectedGroup;
  if (!name) return;
  const next = els.netGroupName.value.trim();
  if (!next || next === name) { closeGroupDialog(); return; }
  if (els.netGroupSave) els.netGroupSave.disabled = true;
  try {
    await jsonApi('/api/network/groups/rename', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, new_name: next }),
    });
    patchGroupLocally(name, next);
    closeGroupDialog();
    renderNetwork();
    toast('Group renamed', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to rename group: ' + (exc.message || exc), 'error');
    }
    if (els.netGroupSave) els.netGroupSave.disabled = false;
  }
}

async function deleteGroup() {
  const name = selectedGroup;
  if (!name) return;
  const members = membersOf((state.network && state.network.devices) || [], name);
  const ok = await confirmAction({
    title: 'Delete group?',
    message: '"' + name + '" is removed. Its ' + members.length +
      (members.length === 1 ? ' device moves' : ' devices move') +
      ' to Unclassified — nothing is deleted from the network.',
    okLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await jsonApi('/api/network/groups/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name }),
    });
    patchGroupLocally(name, '');
    closeGroupDialog();
    renderNetwork();
    toast('Group deleted', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to delete group: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireNetGroupDialog() {
  if (!els.netGroupDialog) return;
  els.netGroupDialogClose.addEventListener('click', closeGroupDialog);
  els.netGroupDialog.addEventListener('click', function (ev) {
    if (ev.target === els.netGroupDialog) closeGroupDialog();  // backdrop
  });
  els.netGroupName.addEventListener('input', function () {
    const next = els.netGroupName.value.trim();
    if (els.netGroupSave) els.netGroupSave.disabled = !next || next === selectedGroup;
  });
  els.netGroupName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveGroupName(); }
  });
  if (els.netGroupSave) els.netGroupSave.addEventListener('click', saveGroupName);
  if (els.netGroupDelete) els.netGroupDelete.addEventListener('click', deleteGroup);
}

// ------------------------------------------------- prefs + toggles
// Persisted list preferences (localStorage), like plugs/security toggles. The
// storage/try-catch concern lives once in state.js (issue #571); what an absent
// or unrecognised stored value means stays here.
const showOfflinePref = persistedFlag(NETWORK_SHOW_OFFLINE_KEY);
const showHiddenDevicesPref = persistedFlag(NETWORK_SHOW_HIDDEN_DEVICES_KEY);
const deviceGroupingPref = persistedPref(NETWORK_DEVICE_GROUPING_KEY);
const deviceSortPref = persistedPref(NETWORK_DEVICE_SORT_KEY);

export function toggleShowOffline() {
  state.networkShowOffline = !state.networkShowOffline;
  showOfflinePref.write(state.networkShowOffline);
  renderNetwork();
}

export function toggleShowHiddenDevices(ev) {
  if (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  state.networkShowHiddenDevices = !state.networkShowHiddenDevices;
  showHiddenDevicesPref.write(state.networkShowHiddenDevices);
  renderNetwork();
}

export function initShowOfflinePref() {
  state.networkShowOffline = showOfflinePref.read();
}

export function initShowHiddenDevicesPref() {
  state.networkShowHiddenDevices = showHiddenDevicesPref.read();
}

export function setDeviceGrouping(grouping) {
  state.networkDeviceGrouping = grouping === 'group' ? 'group' : 'band';
  deviceGroupingPref.write(state.networkDeviceGrouping);
  renderNetwork();
}

// "My groups" is the default for anyone with nothing persisted yet (#519); a
// prior explicit 'band' choice is preserved.
export function initDeviceGroupingPref() {
  state.networkDeviceGrouping = deviceGroupingPref.read() === 'band' ? 'band' : 'group';
}

export function setDeviceSort(sort) {
  state.networkDeviceSort = sort === 'signal' ? 'signal' : 'az';
  deviceSortPref.write(state.networkDeviceSort);
  renderNetwork();
}

export function initDeviceSortPref() {
  state.networkDeviceSort = deviceSortPref.read() === 'signal' ? 'signal' : 'az';
}
