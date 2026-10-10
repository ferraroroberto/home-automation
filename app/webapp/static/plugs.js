/* Smart Life (Tuya) data + the Devices tab's Plugs and Blinds groups.
 *
 * Owns the local Tuya devices: on/off switches, live wattage on metered
 * plugs, and Up / Stop / Down on covers, all on the shared row (#884), plus
 * the Power glance card's live total. All cloud-free — it reads GET /api/tuya
 * (which does per-device LAN reads) and writes the switch/cover endpoints,
 * re-rendering from the read-back.
 *
 * Cadence is tab-aware like energy.js: it polls only while the Devices tab is
 * open (LAN reads are comparatively expensive) and stops on leave. */

'use strict';

import {
  state, els, toast, reportFetchOk, persistedFlag,
  PLUGS_SHOW_OFFLINE_KEY, PLUGS_SHOW_HIDDEN_KEY,
} from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { fmtW, friendlyError } from './format.js';
import { restoreSnapshot, saveSnapshot } from './snapshots.js';
import { createPoller } from './poll.js';
import { createViewState, markTabFailure, renderFeedback } from './view-state.js';
import { toggleMarkup } from './toggle.js';
import { confirmAction } from './confirm.js';
import { detailModal } from './detail-modal.js';
import { setHeadPart } from './head-status.js';
import { rowEl } from './row.js';
import { chipEl } from './chip.js';
import { icon } from './_vendored/icons/icons.js';

// The two list filters, on the shared localStorage wrapper: the Offline
// row's fold (closed by default) and Show hidden.
const showOfflinePref = persistedFlag(PLUGS_SHOW_OFFLINE_KEY, false);
const showHiddenPref = persistedFlag(PLUGS_SHOW_HIDDEN_KEY, false);

const POLL_MS = 15_000;

const plugsView = createViewState('plugs');

function renderPlugsFeedback() {
  renderFeedback(plugsView, els.plugsFeedback, {
    icon: 'plug-zap',
    loadingLabel: 'Reading plugs and blinds…',
    emptyLabel: 'No Smart Life devices configured',
    errorLabel: 'Plugs and blinds unavailable',
    snapshotKey: 'plugs',
    onRetry: function () { loadPlugs(); },
  });
}

function markPlugsFailure() {
  markTabFailure(plugsView, {
    hasData: state.plugs.length > 0,
    scope: 'plugs',
    label: 'plugs',
    render: renderPlugs,
  });
}

// --------------------------------------------------------------- formatting
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

// Exported for the Lights card, which renders the Tuya lights (#181).
export async function toggleSwitch(device, btn) {
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
    reportActionFailure(exc, 'Failed');
  } finally {
    switchBusy.delete(device.device_id);
    // On success renderPlugs() rebuilt the row; on error the old node stays,
    // so re-enable it explicitly.
    if (btn) btn.disabled = false;
  }
}

// Dim a Tuya light (#870). Exported for the Lights card's slider; like the
// Elgato slider it does not toast on success (it fires on every release), and
// re-renders from the read-back card.
export async function setTuyaBrightness(device, pct) {
  try {
    const updated = await jsonApi(
      '/api/tuya/' + encodeURIComponent(device.device_id) + '/brightness',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ brightness: pct }),
      },
    );
    patchPlug(device.device_id, updated);
    renderPlugs();
  } catch (exc) {
    reportActionFailure(exc, 'Brightness failed');
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
    toast(plugLabel(device) + ' ' + BLIND_WORDS[action], 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed');
  }
}

// House-wide group move (#181): every blind the Blinds card currently lists
// (so a user-hidden blind stays out of it), offline-looking ones included — a
// user command bypasses the poll backoff, and the blind may well answer. The
// server sends the commands in parallel and reports each blind's outcome.
// No tap ever waits for an earlier one (#899): a Stop must go out while an
// All up / All down is still in flight (an unresponsive blind holds that
// request open), and the server keeps each blind's commands in tap order.
// Only the latest tap's answer is toasted, so a slow earlier one can't
// overwrite it.
let groupSeq = 0;

async function blindsGroupAction(action) {
  const targets = state.blindsShown;
  if (!targets.length) {
    toast('No blinds to move', 'error');
    return;
  }
  const seq = ++groupSeq;
  try {
    toast('Sending…', 'pending');
    const body = await jsonApi('/api/tuya/covers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: action,
        device_ids: targets.map(function (d) { return d.device_id; }),
      }),
    });
    if (seq !== groupSeq) return;
    const failed = ((body && body.results) || []).filter(function (r) {
      return !r.ok && !r.superseded;
    });
    if (failed.length) {
      const names = failed.map(function (r) {
        const d = deviceById(r.device_id);
        return d ? plugLabel(d) : r.device_id;
      });
      toast(
        failed.length + ' of ' + targets.length + ' blinds failed, retrying: ' + names.join(', '),
        'error',
      );
    } else {
      toast('All blinds ' + BLIND_WORDS[action], 'success');
    }
  } catch (exc) {
    if (seq === groupSeq) reportActionFailure(exc, 'Failed');
  }
}

export function wireBlindsGroup() {
  [[els.blindsAllUp, 'open'], [els.blindsAllStop, 'stop'], [els.blindsAllDown, 'close']]
    .forEach(function (pair) {
      if (pair[0]) pair[0].addEventListener('click', function () { blindsGroupAction(pair[1]); });
    });
}

// ------------------------------------------------------------- the rows
// Every Tuya device is the shared row (row.js, #880) since #884 (Step 6/8 of
// #872): the kind glyph, the name, one muted meta line, and one trailing item
// (a plug's switch, a blind's segmented verb). Tapping the row opens the
// device's rename / hide dialog. An unreachable device says so in words; its
// connection error and address stay out of the copy (#879).

// The power switch, also used by the Lights group for a Tuya light (#181).
export function tuyaSwitch(device, label) {
  const on = device.switch_on === true;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle' + (on ? ' on' : '');
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', on ? 'true' : 'false');
  toggle.setAttribute('aria-label', 'Power ' + (label || plugLabel(device) || 'device'));
  toggle.innerHTML = toggleMarkup(on);
  toggle.addEventListener('click', function () { toggleSwitch(device, toggle); });
  return toggle;
}

// What a plug is doing, in one line: its draw while on, else On / Off.
function plugMeta(device) {
  if (!device.reachable) return 'Not reachable right now';
  const watts = device.metered && device.power_w != null ? fmtW(device.power_w) : '';
  if (!device.has_switch) return watts;
  if (device.switch_on !== true) return 'Off';
  return watts || 'On';
}

// A user-hidden device is listed only while Show hidden is on, so it says
// which one it is (a plain fact, the neutral chip).
function hiddenChip(device) {
  return device.hidden ? chipEl('Hidden', null, 'device-hidden-chip') : null;
}

function plugRow(device) {
  const row = rowEl({
    className: 'plug-row',
    glyph: 'plug-zap',
    title: plugLabel(device) || 'Device',
    meta: plugMeta(device),
    chip: hiddenChip(device),
    onOpen: function (btn) { openPlugDetail(device.device_id, btn); },
    trail: device.reachable && device.has_switch ? tuyaSwitch(device) : null,
  });
  if (!device.reachable) row.classList.add('is-unavailable');
  row.dataset.deviceId = device.device_id;
  return row;
}

// The unreachable plugs fold into this one row (#872 shared system: an
// unplugged plug is not an exception per row). Tapping it lists them under
// it, or folds them away again; the choice persists.
function offlineRow(count) {
  const row = rowEl({
    className: 'plugs-offline-row',
    glyph: 'plug-zap',
    title: 'Offline',
    meta: count === 1 ? 'One plug not reachable' : count + ' plugs not reachable',
    chevron: true,
    onOpen: function () {
      state.plugsShowOffline = !state.plugsShowOffline;
      showOfflinePref.write(state.plugsShowOffline);
      renderPlugs();
      const again = els.plugsList.querySelector('.plugs-offline-row .action-row-main');
      if (again) again.focus();
    },
  });
  const main = row.querySelector('.action-row-main');
  main.setAttribute('aria-expanded', state.plugsShowOffline ? 'true' : 'false');
  main.dataset.testid = 'plugs-offline-toggle';
  return row;
}

// Up · Stop · Down as one segmented verb (decision 6 of #872): the row's one
// trailing item, each segment a real 44px target. Glyphs on the row, the
// household's own word for each (roller blinds go up and down, #181) as the
// accessible name and title; the API keeps Tuya's open / stop / close.
const BLIND_VERBS = [
  ['open', 'Up', 'chevron-up'],
  ['stop', 'Stop', 'square'],
  ['close', 'Down', 'chevron-down'],
];
const BLIND_WORDS = { open: 'up', stop: 'stopped', close: 'down' };

function blindVerbs(name, act) {
  const group = document.createElement('div');
  group.className = 'segmented segmented--row';
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', 'Move ' + name);
  BLIND_VERBS.forEach(function (spec) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item blind-btn';
    btn.dataset.action = spec[0];
    btn.title = spec[1];
    btn.setAttribute('aria-label', spec[1] + ' ' + name);
    btn.innerHTML = icon(spec[2], spec[0] === 'stop' ? 'blind-stop-icon' : '');
    btn.addEventListener('click', function () { act(spec[0]); });
    group.appendChild(btn);
  });
  return group;
}

function blindRow(device) {
  const name = plugLabel(device) || 'Blind';
  const row = rowEl({
    className: 'blind-row',
    glyph: 'blinds',
    badge: device.reachable ? null : 'down',
    title: name,
    chip: device.reachable ? hiddenChip(device) : chipEl('Offline', 'attention', 'blind-offline'),
    onOpen: function (btn) { openPlugDetail(device.device_id, btn); },
    trail: device.reachable ? blindVerbs(name, function (action) { coverAction(device, action); }) : null,
  });
  row.dataset.deviceId = device.device_id;
  return row;
}

// A group's meta is the count it lists; the group hides when it has no
// device at all (`present`, default: something listed).
function setListCard(card, countEl, n, present) {
  if (card) card.hidden = !(present === undefined ? n > 0 : present);
  if (countEl) {
    countEl.textContent = String(n);
    countEl.hidden = n === 0;
  }
}

// --------------------------------------------------------- rename modal
// Detail-modal staging (#203 pattern): the display name and Hidden edits are
// held locally and written only on Save, via the shared detailModal() shell
// (issue #699) — plugModal.staged holds the working toggle state captured
// when the modal opens; closing discards it.
function renderPlugHiddenToggle(hidden) {
  const btn = els.plugHiddenToggle;
  if (!btn) return;
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(hidden);
}

const plugModal = detailModal({
  dialog: els.plugDialog,
  closeButton: els.plugDetailClose,
  onClose: function () { state.selectedPlugId = null; },
  saveButton: els.plugSave,
  focusEl: els.plugDisplayName,
  getEntity: deviceById,
  stage: function (device) { return { hidden: !!device.hidden }; },
  populate: function (staged, device) {
    els.plugDetailName.textContent = plugLabel(device) || 'Device';
    els.plugDisplayName.value = device.display_name || '';
    els.plugDisplayName.placeholder = device.name || 'Custom label…';
    // Original Smart Life name stays visible even with a custom label set, so
    // the device can be matched back to the Smart Life app.
    if (els.plugOriginalName) {
      els.plugOriginalName.textContent = device.name ? 'Original name: ' + device.name : '';
    }
    renderPlugHiddenToggle(staged.hidden);
  },
  buildOps: function (id, staged, device) {
    const ops = [];
    const newName = els.plugDisplayName.value.trim();
    if ((device.display_name || '') !== newName) {
      ops.push(jsonApi('/api/tuya/' + encodeURIComponent(id) + '/display_name', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: newName }),
      }).then(function () { patchPlug(id, { display_name: newName || null }); }));
    }
    if (!!device.hidden !== staged.hidden) {
      ops.push(jsonApi('/api/tuya/' + encodeURIComponent(id) + '/hidden', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: staged.hidden }),
      }).then(function () { patchPlug(id, { hidden: staged.hidden }); }));
    }
    return ops;
  },
  afterSave: function (id) {
    const upd = deviceById(id);
    if (upd) els.plugDetailName.textContent = plugLabel(upd) || 'Device';
  },
  render: renderPlugs,
});

// Exported for the light sheet: a Tuya light's name and Hidden switch are
// this dialog's (#181).
export function openPlugDetail(deviceId, trigger) {
  if (!deviceById(deviceId)) return;
  state.selectedPlugId = deviceId;
  plugModal.open(deviceId, trigger);
}

function togglePlugHidden() {
  if (!plugModal.staged) return;
  plugModal.staged.hidden = !plugModal.staged.hidden;
  renderPlugHiddenToggle(plugModal.staged.hidden);
  plugModal.markDirty();
}

function patchPlug(id, patch) {
  state.plugs = state.plugs.map(function (d) {
    return d.device_id === id ? Object.assign({}, d, patch) : d;
  });
}

// ----------------------------------------------------------- summary stats
// Totals over every known plug (state.plugs, Tuya lights excluded — the
// Lights group counts those, #181), independent of the list filters: switches
// on and live watts on reachable metered plugs. They feed the
// Devices tab's Power glance card (#884), their one home since #885 retired
// Home's plug line.
function renderStats() {
  const devices = state.plugs.filter(function (d) { return !d.is_light; });
  if (!devices.length) {
    if (els.powerNow) els.powerNow.hidden = true;
    if (els.powerOnCount) els.powerOnCount.textContent = '';
    setHeadPart('iot', 'plugs', null);
    return;
  }
  let on = 0;
  let watts = 0;
  const drawing = [];
  devices.forEach(function (d) {
    if (d.switch_on === true) on += 1;
    if (d.metered && d.reachable && d.power_w != null) {
      watts += Number(d.power_w);
      if (Number(d.power_w) > 0) drawing.push(d);
    }
  });
  const wattStr = fmtW(watts);
  const set = function (el, v) { if (el) el.textContent = v; };

  // The glance card: the live total, then the three biggest draws by name.
  set(els.powerOnCount, on + ' on');
  set(els.powerWatts, wattStr);
  drawing.sort(function (a, b) { return Number(b.power_w) - Number(a.power_w); });
  set(els.powerTop, drawing.slice(0, 3).map(function (d) {
    return (plugLabel(d) || 'Device') + ' ' + Math.round(Number(d.power_w));
  }).join(' · '));
  if (els.powerNow) els.powerNow.hidden = false;

  // The Devices header's plain fact (head-status.js, #880): what is on and
  // drawing power, from a live read only.
  setHeadPart('iot', 'plugs', plugsView.state === 'ready'
    ? { fact: on + ' on · ' + wattStr }
    : null);
}

// Show hidden carries the count and only appears when at least one device is
// user-hidden — the same group-foot verb as Security's detectors (#882).
function renderHiddenToggle() {
  const btn = els.plugsHiddenToggle;
  if (!btn) return;
  const n = state.plugsUserHiddenCount || 0;
  btn.hidden = n === 0;
  btn.textContent = state.plugsShowHidden ? 'Hide hidden' : 'Show hidden (' + n + ')';
  btn.setAttribute('aria-pressed', state.plugsShowHidden ? 'true' : 'false');
}

function rowList() {
  const list = document.createElement('ul');
  list.className = 'action-rows';
  return list;
}

export function renderPlugs() {
  els.plugsList.innerHTML = '';
  els.blindsList.innerHTML = '';
  renderPlugsFeedback();
  renderStats();

  if (!state.plugs.length) {
    els.plugsNote.hidden = true;
    state.plugsUserHiddenCount = 0;
    renderHiddenToggle();
    setListCard(els.plugsCard, els.plugsCount, 0);
    setListCard(els.blindsCard, els.blindsCount, 0);
    state.blindsShown = [];
    publishTuyaLights([]);
    return;
  }
  els.plugsNote.hidden = true;

  const sorted = state.plugs.slice().sort(function (a, b) {
    return (a.name || '').localeCompare(b.name || '');
  });

  // User-hidden devices (the per-device Hidden switch) drop out of every list
  // unless Show hidden is on.
  state.plugsUserHiddenCount = sorted.filter(function (d) { return !!d.hidden; }).length;
  const shown = state.plugsShowHidden
    ? sorted
    : sorted.filter(function (d) { return !d.hidden; });
  renderHiddenToggle();

  // Split: covers → Blinds, lights → Lights (#181), everything else → Plugs.
  const isPlug = function (d) { return d.has_cover !== true && !d.is_light; };
  const plugs = shown.filter(isPlug);
  const blinds = shown.filter(function (d) { return d.has_cover === true; });

  const plugList = rowList();
  plugs.filter(function (d) { return d.reachable; })
    .forEach(function (d) { plugList.appendChild(plugRow(d)); });
  const offline = plugs.filter(function (d) { return !d.reachable; });
  if (offline.length) {
    plugList.appendChild(offlineRow(offline.length));
    if (state.plugsShowOffline) offline.forEach(function (d) { plugList.appendChild(plugRow(d)); });
  }
  els.plugsList.appendChild(plugList);

  const blindList = rowList();
  blinds.forEach(function (d) { blindList.appendChild(blindRow(d)); });
  els.blindsList.appendChild(blindList);

  // The Plugs group stays while there is any plug at all, so its Show hidden
  // and Add device remain reachable even when every plug is put away.
  setListCard(els.plugsCard, els.plugsCount, plugs.length, sorted.some(isPlug));
  setListCard(els.blindsCard, els.blindsCount, blinds.length);
  if (els.blindsAllMeta) {
    els.blindsAllMeta.textContent = blinds.length === 1 ? 'One blind' : blinds.length + ' blinds';
  }
  state.blindsShown = blinds;
  publishTuyaLights(shown.filter(function (d) { return d.is_light === true; }));
}

// The Lights card owns the rendering of Tuya lights; hand it the filtered set
// and let it re-render. An event rather than an import keeps the dependency
// one-way (lights.js imports from here, never the reverse).
function publishTuyaLights(lights) {
  state.tuyaLights = lights;
  document.dispatchEvent(new CustomEvent('plugs:rendered'));
}

// ------------------------------------------------------- toggle wiring
export function wirePlugsToggle() {
  // Restore persisted preferences on page load.
  state.plugsShowOffline = showOfflinePref.read();
  state.plugsShowHidden = showHiddenPref.read();

  if (els.plugsHiddenToggle) {
    els.plugsHiddenToggle.addEventListener('click', function () {
      state.plugsShowHidden = !state.plugsShowHidden;
      showHiddenPref.write(state.plugsShowHidden);
      renderPlugs();
    });
  }
}

// Shared by the poll/load path and the Add action: take a /api/tuya-shaped
// body, snapshot it, and re-render from it.
function applyPlugsBody(body) {
  reportFetchOk('plugs');
  saveSnapshot('plugs', body);
  state.plugs = (body && body.devices) || [];
  plugsView.set(state.plugs.length ? 'ready' : 'empty', {
    updatedAt: new Date(),
    liveUnavailable: false,
  });
  renderPlugs();
}

// #612: the in-app replacement for a `tinytuya wizard` terminal run, and since
// the Refresh button was retired it is also the LAN-rediscovery path — the
// server pairs *and* rescans in one action. Confirmed because it leaves the LAN
// and talks to the Tuya cloud; it is slow (a cloud round trip plus an ~8s
// broadcast scan), so the button goes disabled with a busy label to signal that
// the wait is expected rather than a hang.
export function wirePlugsPair() {
  const btn = els.plugsPair;
  if (!btn) return;
  const idleLabel = btn.textContent;
  btn.addEventListener('click', async function () {
    const ok = await confirmAction({
      title: 'Add device',
      message: 'Fetch newly-paired devices from the Tuya cloud and rescan the LAN? '
        + 'Pair the plug in the Smart Life app first — this only captures what that '
        + 'account already has.',
      okLabel: 'Sync',
    });
    if (!ok) return;
    btn.disabled = true;
    btn.textContent = 'Syncing…';
    try {
      const body = await jsonApi('/api/tuya/pair', { method: 'POST' });
      applyPlugsBody(body);
      const info = (body && body.pair) || {};
      const changed = (info.added && info.added.length) || (info.recovered && info.recovered.length);
      toast(friendlyError(info.detail, 'Tuya sync finished'), changed ? 'success' : '');
    } catch (exc) {
      if (!isAuthRequired(exc)) {
        markPlugsFailure();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = idleLabel;
    }
  });
}

// Wire the rename modal once at boot (mirrors the AC detail-modal wiring).
export function wirePlugDetail() {
  // #203: the name + Hidden edits commit on Save, not on blur/toggle.
  els.plugDisplayName.addEventListener('input', plugModal.markDirty);
  els.plugDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); plugModal.save(); }
  });
  if (els.plugHiddenToggle) els.plugHiddenToggle.addEventListener('click', togglePlugHidden);
  if (els.plugSave) els.plugSave.addEventListener('click', plugModal.save);
}

export async function loadPlugs() {
  if (!state.plugs.length) {
    plugsView.set('loading', { liveUnavailable: false });
    renderPlugs();
  }
  try {
    applyPlugsBody(await jsonApi('/api/tuya'));
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markPlugsFailure();
  }
}

export function restorePlugsSnapshot() {
  const body = restoreSnapshot('plugs');
  if (!body) return;
  state.plugs = (body && body.devices) || [];
  plugsView.set(state.plugs.length ? 'stale' : 'empty', {
    updatedAt: state.snapshotUpdatedAt.plugs,
    liveUnavailable: false,
  });
  renderPlugs();
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadPlugs);

// Called by the tab switcher whenever the active tab changes. LAN reads are
// expensive, so only poll while the IoT tab is open; stop when it isn't.
export function onPlugsTab(tab) {
  if (tab === 'iot') {
    loadPlugs();            // immediate refresh on entry (also the first load)
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
