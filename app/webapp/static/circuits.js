/* Circuits (per-breaker CT clamps) data + card controller.
 *
 * Owns the IoT tab's Circuits card: one section per Athom BL0906 meter, and
 * under it EVERY channel that meter has — clamp fitted or not. Reads
 * GET /api/circuits and writes the per-channel rename / sign-flip endpoints.
 *
 * Two deliberate behaviours, both because clamps get added over time:
 *  - a channel is never hidden for reading 0 W, so a clamp fitted next week
 *    starts showing a live figure with nothing to reconfigure;
 *  - meters are discovered server-side over mDNS, so a new meter simply
 *    appears here on its own.
 *
 * Cadence is tab-aware like plugs.js: poll only while the IoT tab is open. */

'use strict';

import { state, els, toast, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { fmtW } from './format.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';
import { closeDialog, openDialog } from './dialog.js';

const POLL_MS = 15_000;

// ------------------------------------------------------------- lookups
function allChannels() {
  return state.circuits.flatMap(function (meter) {
    return (meter.channels || []).map(function (channel) {
      return { meter: meter, channel: channel };
    });
  });
}

function channelByKey(key) {
  return allChannels().find(function (entry) { return entry.channel.key === key; }) || null;
}

function meterByKey(key) {
  return state.circuits.find(function (meter) { return meter.meter_id === key; }) || null;
}

function meterLabel(meter) {
  return meter.display_name || meter.name || meter.meter_id;
}

// An unlabelled channel still needs a stable, meaningful name — "Clamp 3" is
// what is printed next to the terminal on the meter itself.
function channelLabel(channel) {
  return channel.display_name || 'Clamp ' + channel.channel;
}

// ------------------------------------------------------------- row DOM
// One muted caption line per meter, above its channels: which device this is
// and whether it is actually talking to us.
function buildMeterHead(meter) {
  const head = document.createElement('div');
  head.className = 'circuit-meter';
  head.dataset.meterId = meter.meter_id;

  // Tappable like a channel name: with three meters on the way, "cuadro
  // principal" beats "Athom Energy Monitor ddee01".
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'circuit-meter-name';
  name.title = 'Rename this meter';
  name.textContent = meterLabel(meter);
  name.addEventListener('click', function () { openCircuitDetail(meter.meter_id); });
  head.appendChild(name);

  const detail = document.createElement('span');
  detail.className = 'circuit-meter-detail';
  if (!meter.reachable) {
    detail.textContent = 'offline';
    detail.title = meter.error || 'No response on the LAN.';
    head.classList.add('is-unavailable');
  } else {
    const bits = [];
    if (meter.total_power_w != null) bits.push(fmtW(meter.total_power_w));
    if (meter.voltage_v != null) bits.push(meter.voltage_v.toFixed(0) + ' V');
    if (meter.wifi_rssi_dbm != null) bits.push(meter.wifi_rssi_dbm.toFixed(0) + ' dBm');
    detail.textContent = bits.join(' · ');
    detail.title = meter.model || '';
  }
  head.appendChild(detail);
  return head;
}

function buildChannelRow(meter, channel) {
  const row = document.createElement('div');
  row.className = 'device-row circuit-row';
  row.dataset.channelKey = channel.key;

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'device-row-name';
  name.title = 'Rename / fix clamp direction';
  name.textContent = channelLabel(channel);
  name.addEventListener('click', function () { openCircuitDetail(channel.key); });
  row.appendChild(name);

  // A meter that is offline still lists its channels (so circuits don't vanish
  // mid-watch), but they carry no readings — say so rather than showing 0 W.
  if (!meter.reachable || channel.power_w == null) {
    row.classList.add('is-unavailable');
    const note = document.createElement('span');
    note.className = 'device-row-note';
    note.textContent = meter.reachable ? 'no reading' : 'offline';
    row.appendChild(note);
    return row;
  }

  // Current + cumulative energy as a caption; power is the headline figure.
  const bits = [];
  if (channel.current_a != null) bits.push(channel.current_a.toFixed(2) + ' A');
  if (channel.energy_kwh != null) bits.push(channel.energy_kwh.toFixed(2) + ' kWh');
  if (bits.length) {
    const note = document.createElement('span');
    note.className = 'device-row-note';
    note.textContent = bits.join(' · ');
    row.appendChild(note);
  }

  const watts = document.createElement('span');
  watts.className = 'plug-watts';
  watts.textContent = fmtW(channel.power_w);
  // A channel reading negative after the correction is applied is worth
  // flagging: on a load circuit it means the clamp direction is still wrong.
  if (channel.power_w < 0) {
    watts.classList.add('circuit-watts-negative');
    watts.title = 'Reading negative — the clamp may be fitted backwards. '
      + 'Tap the name to flip it.';
  }
  // An idle circuit and a channel with no clamp both read 0 W and are
  // indistinguishable electrically, so neither is dressed up as the other.
  if (channel.power_w === 0) row.classList.add('is-off');
  row.appendChild(watts);
  return row;
}

// --------------------------------------------------------- rename modal
// Staged like the plug modal (#203 pattern): the label and the sign flip are
// held locally and written only on Save.
let circuitStaged = null;

function markCircuitDirty() {
  if (els.circuitSave) els.circuitSave.disabled = false;
}

function clearCircuitDirty() {
  if (els.circuitSave) els.circuitSave.disabled = true;
}

function renderInvertToggle(inverted) {
  const btn = els.circuitInvertToggle;
  if (!btn) return;
  btn.className = 'toggle' + (inverted ? ' on' : ' off');
  btn.setAttribute('aria-checked', inverted ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(inverted);
}

// One dialog serves both a channel and a whole meter: the key tells them apart
// (a channel key is its meter's id plus ":<channel>"), and a meter simply has
// no clamp direction to correct, so that section is hidden for it.
function openCircuitDetail(key) {
  const entry = channelByKey(key);
  const meter = entry ? null : meterByKey(key);
  if (!entry && !meter) return;
  state.selectedCircuitKey = key;

  if (els.circuitInvertSection) els.circuitInvertSection.hidden = !entry;

  if (meter) {
    els.circuitDetailName.textContent = meterLabel(meter);
    els.circuitDisplayName.value = meter.display_name || '';
    els.circuitDisplayName.placeholder = meter.name || 'Custom label…';
    if (els.circuitOriginalName) {
      els.circuitOriginalName.textContent =
        (meter.name || meter.meter_id) + (meter.host ? ' · ' + meter.host : '');
    }
    circuitStaged = { invert: false };
  } else {
    els.circuitDetailName.textContent = channelLabel(entry.channel);
    els.circuitDisplayName.value = entry.channel.display_name || '';
    els.circuitDisplayName.placeholder = 'Clamp ' + entry.channel.channel;
    if (els.circuitOriginalName) {
      // Which physical terminal this is, so an unlabelled clamp stays traceable.
      els.circuitOriginalName.textContent =
        meterLabel(entry.meter) + ' · channel ' + entry.channel.channel;
    }
    circuitStaged = { invert: !!entry.channel.inverted };
    renderInvertToggle(circuitStaged.invert);
  }
  clearCircuitDirty();
  openDialog(els.circuitDialog);
  els.circuitDisplayName.focus();
}

function closeCircuitDetail() {
  state.selectedCircuitKey = null;
  circuitStaged = null;
  clearCircuitDirty();
  closeDialog(els.circuitDialog);
}

function toggleCircuitInvert() {
  if (!circuitStaged) return;
  circuitStaged.invert = !circuitStaged.invert;
  renderInvertToggle(circuitStaged.invert);
  markCircuitDirty();
}

function patchChannel(key, patch) {
  state.circuits = state.circuits.map(function (meter) {
    return Object.assign({}, meter, {
      channels: (meter.channels || []).map(function (channel) {
        return channel.key === key ? Object.assign({}, channel, patch) : channel;
      }),
    });
  });
}

function patchMeter(key, patch) {
  state.circuits = state.circuits.map(function (meter) {
    return meter.meter_id === key ? Object.assign({}, meter, patch) : meter;
  });
}

async function saveCircuitDetail() {
  const key = state.selectedCircuitKey;
  if (!key || !circuitStaged) return;
  const entry = channelByKey(key);
  const meter = entry ? null : meterByKey(key);
  if (!entry && !meter) return;
  if (els.circuitSave) els.circuitSave.disabled = true;
  const newName = els.circuitDisplayName.value.trim();
  const currentName = (entry ? entry.channel.display_name : meter.display_name) || '';
  const ops = [];
  if (currentName !== newName) {
    ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/display_name', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    }).then(function () {
      const patch = { display_name: newName || null };
      if (entry) patchChannel(key, patch); else patchMeter(key, patch);
    }));
  }
  // Only a channel has a clamp direction; a meter never sends this.
  const signChanged = !!entry && !!entry.channel.inverted !== circuitStaged.invert;
  if (signChanged) {
    ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/invert', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ invert: circuitStaged.invert }),
    }).then(function () { patchChannel(key, { inverted: circuitStaged.invert }); }));
  }
  try {
    await Promise.all(ops);
    const updatedChannel = channelByKey(key);
    const updatedMeter = meterByKey(key);
    if (updatedChannel) els.circuitDetailName.textContent = channelLabel(updatedChannel.channel);
    else if (updatedMeter) els.circuitDetailName.textContent = meterLabel(updatedMeter);
    renderCircuits();
    clearCircuitDirty();
    toast('Saved', 'success');
    // A sign flip changes what the server reports, so pull a fresh read rather
    // than leaving the old sign on screen until the next poll.
    if (signChanged) loadCircuits();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save: ' + (exc.message || exc), 'error');
    }
    if (els.circuitSave) els.circuitSave.disabled = false;
  }
}

// ------------------------------------------------------------- render
function setNote(message) {
  if (!els.circuitsNote) return;
  els.circuitsNote.textContent = message || '';
  els.circuitsNote.hidden = !message;
}

export function renderCircuits() {
  if (!els.circuitsList) return;
  els.circuitsList.innerHTML = '';

  const channels = allChannels();
  if (els.circuitsCount) {
    els.circuitsCount.textContent = String(channels.length);
    els.circuitsCount.hidden = channels.length === 0;
  }

  if (!state.circuits.length) {
    // The card stays visible with an explanation rather than disappearing:
    // "no meters found" is a state worth seeing, not an empty space.
    setNote(
      state.circuitsError
      || 'No CT-clamp meters found yet. They are discovered automatically once '
         + 'powered and joined to Wi-Fi.',
    );
    return;
  }
  setNote(state.circuitsError || '');

  // One meter at a time, its channels in physical order (1..N) — the order
  // they are printed on the meter's own terminal block.
  state.circuits.forEach(function (meter) {
    els.circuitsList.appendChild(buildMeterHead(meter));
    const ordered = (meter.channels || []).slice().sort(function (a, b) {
      return a.channel - b.channel;
    });
    ordered.forEach(function (channel) {
      els.circuitsList.appendChild(buildChannelRow(meter, channel));
    });
  });
}

// --------------------------------------------------------------- load
export async function loadCircuits() {
  try {
    const body = await jsonApi('/api/circuits');
    reportFetchOk('circuits');
    state.circuits = (body && body.meters) || [];
    // A discovery problem is reported as its own fact, never folded into
    // "no meters" — the two need different actions from whoever is reading.
    state.circuitsError = (body && body.error) || '';
    renderCircuits();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.circuitsError = 'Circuits unavailable: ' + (exc.message || exc);
    renderCircuits();
  }
}

export function wireCircuitsRefresh() {
  if (!els.circuitsRefresh) return;
  els.circuitsRefresh.addEventListener('click', async function () {
    // A refresh re-runs mDNS discovery server-side (a few seconds), so say the
    // wait is expected rather than looking hung.
    els.circuitsRefresh.disabled = true;
    els.circuitsRefresh.textContent = 'Scanning…';
    try {
      const body = await jsonApi('/api/circuits/refresh', { method: 'POST' });
      reportFetchOk('circuits');
      state.circuits = (body && body.meters) || [];
      state.circuitsError = (body && body.error) || '';
      renderCircuits();
      toast((body && body.refresh && body.refresh.detail) || 'Circuits refreshed', '');
    } catch (exc) {
      if (String(exc.message) !== 'auth required') {
        toast('Failed: ' + (exc.message || exc), 'error');
      }
    } finally {
      els.circuitsRefresh.disabled = false;
      els.circuitsRefresh.textContent = 'Refresh';
    }
  });
}

// Wire the rename modal once at boot (mirrors the plug detail-modal wiring).
export function wireCircuitDetail() {
  if (!els.circuitDialog) return;
  els.circuitDetailClose.addEventListener('click', closeCircuitDetail);
  els.circuitDialog.addEventListener('click', function (ev) {
    if (ev.target === els.circuitDialog) closeCircuitDetail();  // backdrop click
  });
  els.circuitDisplayName.addEventListener('input', markCircuitDirty);
  els.circuitDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveCircuitDetail(); }
  });
  if (els.circuitInvertToggle) {
    els.circuitInvertToggle.addEventListener('click', toggleCircuitInvert);
  }
  if (els.circuitSave) els.circuitSave.addEventListener('click', saveCircuitDetail);
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadCircuits);

export function onCircuitsTab(tab) {
  if (tab === 'iot') {
    loadCircuits();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
