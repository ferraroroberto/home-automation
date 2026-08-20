/* Circuits (per-breaker CT clamps) data + card controller.
 *
 * Owns the IoT tab's Circuits card: one foldable group per Athom BL0906 meter,
 * and under it EVERY channel that meter has — clamp fitted or not. Reads
 * GET /api/circuits and writes the per-channel rename / sign-flip / hide
 * endpoints.
 *
 * Two deliberate behaviours, both because clamps get added over time:
 *  - a channel is never dropped for reading 0 W, so a clamp fitted next week
 *    starts showing a live figure with nothing to reconfigure. Hiding one is a
 *    *user* decision (issue #619), never inferred from a reading;
 *  - meters are discovered server-side over mDNS, so a new meter simply
 *    appears here on its own — which is why this card has no Refresh button.
 *
 * One number per row (issue #619): watts. Amps, cumulative kWh, mains voltage,
 * Wi-Fi signal and the meter's MAC are reference figures, so they live in the
 * detail dialog where you go looking for them deliberately.
 *
 * Cadence is tab-aware like plugs.js: poll only while the IoT tab is open. */

'use strict';

import {
  state, els, toast, reportFetchOk, persistedFlag,
  CIRCUITS_COLLAPSED_KEY, CIRCUITS_SHOW_HIDDEN_KEY,
} from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { fmtW } from './format.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';
import { closeDialog, openDialog } from './dialog.js';
import { icon } from './_vendored/icons/icons.js';

const POLL_MS = 15_000;

// Which meter groups are folded shut. renderCircuits() rebuilds the whole list
// on every poll, so this cannot live in the DOM the way voice-commands.js's
// groups can — that card renders once. Held here and re-applied each render.
const collapsedMeters = new Set();

// The "show hidden terminals" filter, on the shared localStorage wrapper.
const showHiddenPref = persistedFlag(CIRCUITS_SHOW_HIDDEN_KEY, false);

function loadCollapsedMeters() {
  try {
    const raw = JSON.parse(localStorage.getItem(CIRCUITS_COLLAPSED_KEY) || '[]');
    if (Array.isArray(raw)) raw.forEach(function (id) { collapsedMeters.add(String(id)); });
  } catch (_) { /* private mode, or a hand-mangled value — start expanded */ }
}

function saveCollapsedMeters() {
  try {
    localStorage.setItem(CIRCUITS_COLLAPSED_KEY, JSON.stringify(Array.from(collapsedMeters)));
  } catch (_) { /* private mode */ }
}

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

// A→Z on the label actually on screen, numeric-aware — so renaming meters
// "1 …", "2 …", "10 …" orders the board the obvious way instead of lexically
// (and instead of mDNS discovery order, which is arbitrary).
function byMeterLabel(a, b) {
  return meterLabel(a).localeCompare(meterLabel(b), undefined, {
    numeric: true, sensitivity: 'base',
  });
}

// Physical terminal order (1..N — the order printed on the meter's own terminal
// block), minus anything the user has put away.
function visibleChannels(meter) {
  const ordered = (meter.channels || []).slice().sort(function (a, b) {
    return a.channel - b.channel;
  });
  if (state.circuitsShowHidden) return ordered;
  return ordered.filter(function (channel) { return !channel.hidden; });
}

// ------------------------------------------------------------- row DOM
// One foldable group per meter (issue #619). The header carries the meter's
// name and nothing else: this card exists to show individual circuits, so the
// meter's own aggregate would be the one figure on screen nobody asked for.
// Its reference data (voltage, total, signal, MAC) is in the dialog instead.
function buildMeterGroup(meter) {
  const group = document.createElement('details');
  group.className = 'circuit-group';
  group.dataset.meterId = meter.meter_id;
  group.open = !collapsedMeters.has(meter.meter_id);

  const summary = document.createElement('summary');
  summary.className = 'collapse-summary circuit-meter';

  const main = document.createElement('span');
  main.className = 'collapse-main';

  // Tappable like a channel name: with three meters on the way, "cuadro
  // principal" beats "Athom Energy Monitor ddee01". The summary-embedded-control
  // pattern (as on the HA card's power switch) — this click edits the meter and
  // must never fold the group, so it stops the summary's default toggle.
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'circuit-meter-name';
  name.title = 'Rename this meter';
  name.textContent = meterLabel(meter);
  name.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    openCircuitDetail(meter.meter_id);
  });
  main.appendChild(name);

  // "offline" is a state, not a reading — the readings left this row in #619,
  // but why every channel below reads nothing has to stay visible, including
  // while the group is folded shut.
  if (!meter.reachable) {
    summary.classList.add('is-unavailable');
    const note = document.createElement('span');
    note.className = 'circuit-meter-detail';
    note.textContent = 'offline';
    note.title = meter.error || 'No response on the LAN.';
    main.appendChild(note);
  }

  summary.appendChild(main);
  // No leading glyph: the header is deliberately just the name, and the chevron
  // alone carries the disclosure affordance.
  summary.insertAdjacentHTML('beforeend', icon('chevron-right', 'collapse-chevron'));
  group.appendChild(summary);

  group.addEventListener('toggle', function () {
    if (group.open) collapsedMeters.delete(meter.meter_id);
    else collapsedMeters.add(meter.meter_id);
    saveCollapsedMeters();
  });

  const body = document.createElement('div');
  body.className = 'circuit-group-body';
  visibleChannels(meter).forEach(function (channel) {
    body.appendChild(buildChannelRow(meter, channel));
  });
  group.appendChild(body);
  return group;
}

function buildChannelRow(meter, channel) {
  const row = document.createElement('div');
  row.className = 'device-row circuit-row';
  row.dataset.channelKey = channel.key;

  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'device-row-name';
  name.title = 'Readings / rename / fix clamp direction';
  name.textContent = channelLabel(channel);
  name.addEventListener('click', function () { openCircuitDetail(channel.key); });
  row.appendChild(name);

  // Only visible while "Show hidden" is on, so it is worth saying which rows
  // are the ones normally put away.
  if (channel.hidden) {
    row.classList.add('is-hidden-circuit');
    const flag = document.createElement('span');
    flag.className = 'device-row-note';
    flag.textContent = 'hidden';
    row.appendChild(flag);
  }

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

  // Watts is the whole row (issue #619): amps and cumulative kWh moved into the
  // dialog, because six rows of three figures each stop being scannable.
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

function renderToggle(btn, on, label) {
  if (!btn) return;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
  if (label) btn.setAttribute('aria-label', label);
}

// "—" rather than a blank cell: an absent reading is a fact worth showing.
function setReading(el, value, unit, digits) {
  if (!el) return;
  el.textContent = value == null ? '—' : value.toFixed(digits) + ' ' + unit;
}

// One dialog serves both a channel and a whole meter: the key tells them apart
// (a channel key is its meter's id plus ":<channel>"). A meter has no clamp
// direction to correct, and hiding one would hide every circuit under it, so
// both toggle sections are for a channel only — and each gets the read-only
// block that suits it.
function openCircuitDetail(key) {
  const entry = channelByKey(key);
  const meter = entry ? null : meterByKey(key);
  if (!entry && !meter) return;
  state.selectedCircuitKey = key;

  if (els.circuitInvertSection) els.circuitInvertSection.hidden = !entry;
  if (els.circuitHiddenSection) els.circuitHiddenSection.hidden = !entry;
  if (els.circuitReadings) els.circuitReadings.hidden = !entry;
  if (els.circuitMeterInfo) els.circuitMeterInfo.hidden = !meter;

  if (meter) {
    els.circuitDetailName.textContent = meterLabel(meter);
    els.circuitDisplayName.value = meter.display_name || '';
    els.circuitDisplayName.placeholder = meter.name || 'Custom label…';
    if (els.circuitOriginalName) {
      els.circuitOriginalName.textContent =
        (meter.name || meter.meter_id) + (meter.host ? ' · ' + meter.host : '');
    }
    setReading(els.circuitMeterVoltage, meter.voltage_v, 'V', 0);
    // fmtW already renders a missing value as the same em dash setReading uses.
    if (els.circuitMeterTotal) els.circuitMeterTotal.textContent = fmtW(meter.total_power_w);
    setReading(els.circuitMeterSignal, meter.wifi_rssi_dbm, 'dBm', 0);
    if (els.circuitMeterMac) {
      // Statically-configured meters have no MAC until read — the server sends
      // null rather than dressing "host:<ip>" up as one.
      els.circuitMeterMac.textContent = meter.mac || '—';
    }
    circuitStaged = { invert: false, hidden: false };
  } else {
    els.circuitDetailName.textContent = channelLabel(entry.channel);
    els.circuitDisplayName.value = entry.channel.display_name || '';
    els.circuitDisplayName.placeholder = 'Clamp ' + entry.channel.channel;
    if (els.circuitOriginalName) {
      // Which physical terminal this is, so an unlabelled clamp stays traceable.
      els.circuitOriginalName.textContent =
        meterLabel(entry.meter) + ' · channel ' + entry.channel.channel;
    }
    if (els.circuitReadingPower) {
      els.circuitReadingPower.textContent = fmtW(entry.channel.power_w);
    }
    setReading(els.circuitReadingCurrent, entry.channel.current_a, 'A', 2);
    setReading(els.circuitReadingEnergy, entry.channel.energy_kwh, 'kWh', 2);
    circuitStaged = {
      invert: !!entry.channel.inverted,
      hidden: !!entry.channel.hidden,
    };
    renderToggle(els.circuitInvertToggle, circuitStaged.invert);
    renderToggle(els.circuitHiddenToggle, circuitStaged.hidden);
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
  renderToggle(els.circuitInvertToggle, circuitStaged.invert);
  markCircuitDirty();
}

function toggleCircuitHidden() {
  if (!circuitStaged) return;
  circuitStaged.hidden = !circuitStaged.hidden;
  renderToggle(els.circuitHiddenToggle, circuitStaged.hidden);
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
  // Only a channel has a clamp direction or a hidden flag; a meter never sends
  // either of these.
  const signChanged = !!entry && !!entry.channel.inverted !== circuitStaged.invert;
  if (signChanged) {
    ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/invert', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ invert: circuitStaged.invert }),
    }).then(function () { patchChannel(key, { inverted: circuitStaged.invert }); }));
  }
  if (!!entry && !!entry.channel.hidden !== circuitStaged.hidden) {
    ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/hidden', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden: circuitStaged.hidden }),
    }).then(function () { patchChannel(key, { hidden: circuitStaged.hidden }); }));
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
    reportActionFailure(exc, 'Failed to save');
    if (els.circuitSave) els.circuitSave.disabled = false;
  }
}

// ------------------------------------------------------------- render
function setNote(message) {
  if (!els.circuitsNote) return;
  els.circuitsNote.textContent = message || '';
  els.circuitsNote.hidden = !message;
}

// The "Show hidden" affordance only exists once something is actually put away
// — same rule as the Plugs card. It lives in the card's own summary beside the
// chevron, so it stays reachable even with the card folded.
function renderHiddenToggle() {
  const n = state.circuitsHiddenCount || 0;
  const btn = els.circuitsHiddenToggle;
  if (!btn) return;
  btn.hidden = n === 0;
  btn.textContent = state.circuitsShowHidden ? 'Hide hidden' : 'Show hidden (' + n + ')';
  btn.classList.toggle('active', state.circuitsShowHidden);
  btn.setAttribute('aria-pressed', state.circuitsShowHidden ? 'true' : 'false');
}

export function renderCircuits() {
  if (!els.circuitsList) return;
  els.circuitsList.innerHTML = '';

  const channels = allChannels();
  state.circuitsHiddenCount = channels.filter(function (entry) {
    return entry.channel.hidden;
  }).length;
  renderHiddenToggle();

  // The badge counts what is actually drawn, so it agrees with the card.
  const shown = state.circuitsShowHidden
    ? channels.length
    : channels.length - state.circuitsHiddenCount;
  if (els.circuitsCount) {
    els.circuitsCount.textContent = String(shown);
    els.circuitsCount.hidden = shown === 0;
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

  // One foldable group per meter, A→Z by name; channels inside stay in physical
  // terminal order. Sorted on a copy — state.circuits mirrors the server body.
  state.circuits.slice().sort(byMeterLabel).forEach(function (meter) {
    els.circuitsList.appendChild(buildMeterGroup(meter));
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
    if (isAuthRequired(exc)) return;
    state.circuitsError = 'Circuits unavailable: ' + (exc.message || exc);
    renderCircuits();
  }
}

// There is no Refresh button (issue #619): the card re-polls on its own while
// the IoT tab is open, and a meter that joins the Wi-Fi appears by itself once
// the server's mDNS discovery TTL lapses. POST /api/circuits/refresh still
// exists for a forced sweep from the command line.
export function wireCircuitsToggle() {
  state.circuitsShowHidden = showHiddenPref.read();
  loadCollapsedMeters();

  if (!els.circuitsHiddenToggle) return;
  els.circuitsHiddenToggle.addEventListener('click', function (ev) {
    // It sits inside the card's <summary>: filtering the list must never be
    // read as a request to fold the card away.
    ev.preventDefault();
    ev.stopPropagation();
    state.circuitsShowHidden = !state.circuitsShowHidden;
    showHiddenPref.write(state.circuitsShowHidden);
    renderCircuits();
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
  if (els.circuitHiddenToggle) {
    els.circuitHiddenToggle.addEventListener('click', toggleCircuitHidden);
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
