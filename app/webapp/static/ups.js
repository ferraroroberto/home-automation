/* Local USB UPS: the Devices tab's UPS row + sheet.
 *
 * Reads GET /api/ups. The backend prefers NUT when available and otherwise uses
 * Windows USB-HID battery telemetry, so the connected PC UPS works without
 * vendor cloud software. On Devices (#884) the UPS is a row of the Power glance
 * card, and its sheet holds the reading and the PC-fleet shutdown (#498). On
 * Home (#885) the UPS is no tile: an exception (on battery, low, critical) is
 * a chip on the House card that opens this sheet, and a healthy UPS says
 * nothing there. */

'use strict';

import { state, els, toast, reportFetchOk } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { emptyStateEl } from './empty-state.js';
import { fmtPct } from './format.js';
import { chipEl } from './chip.js';
import { setHeadPart } from './head-status.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot } from './snapshots.js';
import { loadPowerNotifyPrefs } from './ups-notify.js';
import { createPoller } from './poll.js';
import { createViewState, markTabFailure, staleNoteEl, staleText } from './view-state.js';
import { rowEl } from './row.js';
import { sheet } from './sheet.js';
import { setHousePart } from './home.js';
import { showTab } from './tabs.js';

const POLL_MS = 15_000;

let lastMainsOnline = null;
const upsView = createViewState('ups');

function fmtRuntime(seconds) {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const mins = Math.round(total / 60);
  if (mins < 60) return mins + ' min';
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h + ' h ' + String(m).padStart(2, '0') + ' min';
}

// The UPS status chip, for exceptions only (#879): online, charging and full
// are the normal state and show none. The most urgent condition wins.
function statusChip(ups) {
  const status = (ups && ups.status) || '';
  if (status.indexOf('critical') >= 0) return chipEl('Critical', 'danger', 'ups-status');
  if (status.indexOf('low_battery') >= 0) return chipEl('Low battery', 'danger', 'ups-status');
  if (ups && ups.mains_online === false) return chipEl('On battery', 'attention', 'ups-status');
  return null;
}

// The Power card's UPS row (#884): connected badge, the reading in one line,
// an exception chip, and the sheet one tap away. The row is there in every
// state, so the PC-fleet shutdown in its sheet is always reachable.
function upsRowMeta() {
  if (upsView.state === 'loading') return { text: 'Reading UPS status…', badge: null, chip: null };
  if (upsView.state === 'empty') return { text: 'No UPS detected', badge: null, chip: null };
  if (upsView.state === 'error') {
    return { text: 'Status unavailable', badge: 'down', chip: chipEl('Unavailable', 'attention', 'ups-status') };
  }
  const ups = state.ups;
  const parts = [ups && ups.mains_online === false ? 'On battery' : 'On mains'];
  parts.push(fmtPct(ups && ups.battery_charge_pct));
  parts.push(fmtRuntime(ups && ups.runtime_seconds));
  const chip = statusChip(ups);
  return {
    text: chip ? parts.slice(1).join(' · ') : parts.join(' · '),
    badge: 'up',
    chip: chip,
  };
}

function renderUpsRow() {
  if (!els.upsRows) return;
  const meta = upsRowMeta();
  const row = rowEl({
    className: 'ups-row',
    glyph: 'battery-charging',
    badge: meta.badge,
    title: 'UPS',
    meta: meta.text,
    chip: meta.chip,
    chevron: true,
    onOpen: function (btn) { openUpsSheet(btn); },
  });
  row.dataset.state = upsView.state;
  els.upsRows.innerHTML = '';
  els.upsRows.appendChild(row);
}

function kvRow(label, value) {
  const row = document.createElement('div');
  row.className = 'row';
  const k = document.createElement('span');
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'muted';
  v.textContent = value;
  row.appendChild(k);
  row.appendChild(v);
  return row;
}

function renderUpsSheet() {
  const box = els.upsSheetStatus;
  if (!box) return;
  box.innerHTML = '';
  if (upsView.state === 'loading' || upsView.state === 'empty' || upsView.state === 'error') {
    const empty = upsView.state === 'loading'
      ? emptyStateEl('refresh-cw', 'Reading UPS status…')
      : emptyStateEl('battery-charging',
        upsView.state === 'empty' ? 'No UPS detected' : 'UPS status unavailable',
        { actionLabel: 'Retry', onAction: function () { loadUps(); } });
    box.appendChild(empty);
    return;
  }
  const ups = state.ups;
  let status = ups && ups.mains_online === false ? 'On battery' : 'On mains';
  const chip = statusChip(ups);
  if (chip) status = chip.textContent;
  box.appendChild(kvRow('Status', status));
  box.appendChild(kvRow('Charge', fmtPct(ups && ups.battery_charge_pct)));
  box.appendChild(kvRow('Runtime', fmtRuntime(ups && ups.runtime_seconds)));
  if (upsView.state === 'stale' || isSnapshotRestored('ups')) {
    box.appendChild(staleNoteEl(staleText(upsView, 'ups'), 'ups-stale-note'));
  }
}

const upsSheet = sheet(els.upsSheet, {
  model: 'instant',
  closeButton: els.upsSheetClose,
  doneButton: els.upsSheetDone,
  fallbackFocus: function () {
    return els.upsRows ? els.upsRows.querySelector('.action-row-main') : null;
  },
});

function openUpsSheet(trigger) {
  renderUpsSheet();
  upsSheet.open(trigger);
}

export function renderUps() {
  renderUpsRow();
  if (upsSheet.isOpen()) renderUpsSheet();
  renderUpsHead();
}

// The UPS's part of the Devices header line (head-status.js, #880) and of
// Home's House card (#885): only its exceptions, the same ones the row chips;
// a healthy UPS adds nothing. On Home the chip opens the UPS sheet on Devices.
function renderUpsHead() {
  const ups = state.ups;
  const status = (ups && ups.status) || '';
  let exception = null;
  if (upsView.state === 'ready' && ups && ups.available === true) {
    if (status.indexOf('critical') >= 0) exception = { text: 'UPS critical', tone: 'danger' };
    else if (status.indexOf('low_battery') >= 0) exception = { text: 'UPS battery low', tone: 'danger' };
    else if (ups.mains_online === false) exception = { text: 'On battery', tone: 'attention' };
  }
  setHeadPart('iot', 'ups', exception ? { exceptions: [exception] } : null);
  setHousePart('ups', exception ? [{
    text: exception.text,
    tone: exception.tone,
    label: exception.text + ': open the UPS',
    onOpen: function () {
      showTab('iot');
      openUpsSheet(els.upsRows ? els.upsRows.querySelector('.action-row-main') : null);
    },
  }] : null);
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
    toast('Power restored: UPS is back on mains', 'success');
  }
  lastMainsOnline = next.mains_online;
}

export async function loadUps() {
  if (!state.ups) {
    upsView.set('loading', { liveUnavailable: false });
    renderUps();
  }
  try {
    const body = await jsonApi('/api/ups');
    reportFetchOk('ups');
    saveSnapshot('ups', body);
    state.ups = (body && body.ups) || null;
    upsView.set(state.ups && state.ups.available === true ? 'ready' : 'empty', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    handleTransition(state.ups);
    renderUps();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markTabFailure(upsView, {
      hasData: !!(state.ups && state.ups.available === true),
      scope: 'ups',
      label: 'UPS',
      render: renderUps,
    });
  }
}

export function restoreUpsSnapshot() {
  const body = restoreSnapshot('ups');
  if (!body) return;
  state.ups = (body && body.ups) || null;
  upsView.set(state.ups && state.ups.available === true ? 'stale' : 'empty', {
    updatedAt: state.snapshotUpdatedAt.ups,
    liveUnavailable: false,
  });
  renderUps();
}

const schedule = createPoller(loadUps);

export function onUpsTab(tab) {
  if (tab === 'iot' || tab === 'home') {
    loadUps();
    schedule(tab === 'iot' ? POLL_MS : 0);
  } else {
    schedule(0);
  }
  // The UPS notification toggles live in Settings' Notifications card (#779).
  if (tab === 'settings') loadPowerNotifyPrefs();
}
